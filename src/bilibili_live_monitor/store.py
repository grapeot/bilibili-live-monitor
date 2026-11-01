import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS rooms(room_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS raw_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL,
                session_id TEXT, received_utc TEXT NOT NULL, command TEXT, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS raw_room_time ON raw_events(room_id,received_utc);
            CREATE TABLE IF NOT EXISTS guard_snapshots(
                id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL,
                observed_utc TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS segments(
                id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL, seq INTEGER NOT NULL,
                data TEXT NOT NULL, UNIQUE(room_id,seq));
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS timeline(
                id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL,
                kind TEXT NOT NULL, source_key TEXT NOT NULL, at_utc TEXT NOT NULL,
                data TEXT NOT NULL, UNIQUE(room_id,kind,source_key));
            CREATE INDEX IF NOT EXISTS timeline_room_time ON timeline(room_id,at_utc,id);
            CREATE INDEX IF NOT EXISTS timeline_room_received
                ON timeline(room_id,json_extract(data,'$.received_utc'),id);
        ''')
        # 旧数据库升级时补齐转写时间线；不触发历史SSE重放。
        self.db.execute('''INSERT OR IGNORE INTO timeline(room_id,kind,source_key,at_utc,data)
            SELECT room_id,'transcript',CAST(seq AS TEXT),COALESCE(json_extract(data,'$.end_utc'),?),
            json_set(data,'$.kind','transcript','$.at_utc',COALESCE(json_extract(data,'$.end_utc'),?),
                     '$.received_utc',COALESCE(json_extract(data,'$.end_utc'),?)) FROM segments''',
                        (utc_now(), utc_now(), utc_now()))
        self.db.commit()
        for room in self.rooms():
            if room['status'] != 'stopped':
                room['status'] = 'stopped'
                self.room(room)
        pending = self.db.execute(
            "SELECT data FROM segments WHERE json_extract(data,'$.status') IN ('queued','transcribing')"
        ).fetchall()
        for row in pending:
            segment = json.loads(row['data'])
            if segment.get('status') in ('queued', 'transcribing'):
                segment.update(status='cancelled', is_final=True, error='服务重启，中断的音频不自动重试')
                self.segment(segment)

    def room(self, data):
        data['updated_at'] = utc_now()
        self.db.execute('INSERT OR REPLACE INTO rooms VALUES(?,?)',
                        (data['room_id'], json.dumps(data, ensure_ascii=False)))
        self.db.commit()
        self.event('room', data)

    def rooms(self):
        return [json.loads(r['data']) for r in self.db.execute('SELECT data FROM rooms')]

    def segment(self, data):
        self.db.execute('INSERT OR REPLACE INTO segments(room_id,seq,data) VALUES(?,?,?)',
                        (data['room_id'], data['seq'], json.dumps(data, ensure_ascii=False)))
        self.db.commit()
        self.event('segment', data)
        self.item({**data, 'kind': 'transcript', 'at_utc': data.get('end_utc') or utc_now()},
                  str(data['seq']))

    def item(self, data, source_key):
        data = {**data, 'received_utc': data.get('received_utc') or utc_now()}
        parameters = (data['room_id'], data['kind'], source_key, data['at_utc'],
                      json.dumps(data, ensure_ascii=False))
        action = ('ON CONFLICT(room_id,kind,source_key) DO UPDATE SET data=excluded.data,at_utc=excluded.at_utc'
                  if data['kind'] == 'transcript' else 'ON CONFLICT(room_id,kind,source_key) DO NOTHING')
        cursor = self.db.execute(
            'INSERT INTO timeline(room_id,kind,source_key,at_utc,data) VALUES(?,?,?,?,?) ' + action,
            parameters)
        self.db.commit()
        if cursor.rowcount:
            row = self.db.execute('SELECT id,data FROM timeline WHERE room_id=? AND kind=? AND source_key=?',
                                  parameters[:3]).fetchone()
            self.event('item', {**json.loads(row['data']), 'id': row['id']})

    def danmaku(self, room_id, message):
        self.item({**message, 'room_id': room_id, 'kind': 'danmaku'}, message['source_key'])

    def live_message(self, room_id, message):
        now = utc_now()
        kind = message.get('kind')
        if kind in ('raw_event', 'transport'):
            health = self.control('message_health', {})
            current = health.setdefault(room_id, {})
            if kind == 'raw_event':
                packet = message['packet']
                self.db.execute('INSERT INTO raw_events(room_id,session_id,received_utc,command,data) VALUES(?,?,?,?,?)',
                                (room_id, message.get('session_id'), now, packet.get('cmd'),
                                 json.dumps(packet, ensure_ascii=False)))
                self.db.commit()
                current['last_message_utc'] = now
                current['raw_events'] = current.get('raw_events', 0) + 1
            else:
                changed = current.get('state') != message['state']
                current.update(state=message['state'], state_changed_utc=now)
                if message.get('frame_received'):
                    current['last_frame_utc'] = now
                if changed:
                    self.event('message_transport', {'room_id': room_id, 'at_utc': now, **message})
            self.save_control('message_health', health)
            return
        if message.get('kind') in ('gift', 'entry'):
            self.item({**message, 'room_id': room_id}, message['source_key'])
        else:
            self.danmaku(room_id, message)

    def guard_snapshot(self, snapshot):
        with self.db:
            self.db.execute('INSERT INTO guard_snapshots(room_id,observed_utc,data) VALUES(?,?,?)',
                            (snapshot['room_id'], snapshot['observed_utc'], json.dumps(snapshot, ensure_ascii=False)))

    def latest_guards(self, room_id):
        row = self.db.execute('SELECT id,data FROM guard_snapshots WHERE room_id=? ORDER BY id DESC LIMIT 1',
                              (room_id,)).fetchone()
        return {'id': row['id'], **json.loads(row['data'])} if row else None

    def items(self, room_id, limit=80):
        rows = self.db.execute('SELECT id,data FROM timeline WHERE room_id=? '
                               "ORDER BY json_extract(data,'$.received_utc') DESC,id DESC LIMIT ?",
                               (room_id, limit)).fetchall()
        items = [{**json.loads(r['data']), 'id': r['id']} for r in rows]
        return sorted(items, key=lambda item: (item['at_utc'], item['id']))

    def export_items(self, room_id=None):
        # 独立只读事务冻结内容及排序键，流式修订不会令同一ID重复/漏导。
        snapshot = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        snapshot.row_factory = sqlite3.Row
        try:
            snapshot.execute('BEGIN')
            cursor = snapshot.execute('SELECT id,data FROM timeline WHERE (? IS NULL OR room_id=?) '
                                      'ORDER BY at_utc,id', (room_id, room_id))
            while rows := cursor.fetchmany(500):
                for row in rows:
                    yield {**json.loads(row['data']), 'id': row['id']}
        finally:
            snapshot.close()

    def segments(self, room_id):
        rows = self.db.execute('SELECT data FROM segments WHERE room_id=? ORDER BY seq DESC LIMIT 20',
                               (room_id,)).fetchall()
        return [json.loads(r['data']) for r in reversed(rows)]

    def next_seq(self, room_id):
        return self.db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM segments WHERE room_id=?',
                               (room_id,)).fetchone()[0]

    def event(self, kind, data):
        self.db.execute('INSERT INTO events(kind,data) VALUES(?,?)',
                        (kind, json.dumps(data, ensure_ascii=False)))
        self.db.commit()

    def events(self, after, limit=100):
        return [dict(r) for r in self.db.execute('SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?',
                                                (after, limit))]

    def cursor(self):
        return self.db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]

    def close(self):
        self.db.close()

    def control(self, key, default=None):
        row = self.db.execute('SELECT data FROM control WHERE key=?', (key,)).fetchone()
        return json.loads(row['data']) if row else default

    def save_control(self, key, data):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',
                            (key, json.dumps(data, ensure_ascii=False)))
