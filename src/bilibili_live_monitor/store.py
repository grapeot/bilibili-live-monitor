import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS rooms(room_id TEXT PRIMARY KEY, data TEXT NOT NULL);
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
                segment.update(status='cancelled', error='服务重启，中断的音频不自动重试')
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
        action = ('ON CONFLICT(room_id,kind,source_key) DO UPDATE SET data=excluded.data'
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

    def items(self, room_id, limit=80):
        rows = self.db.execute('SELECT id,data FROM timeline WHERE room_id=? '
                               'ORDER BY at_utc DESC,id DESC LIMIT ?', (room_id, limit)).fetchall()
        return [{**json.loads(r['data']), 'id': r['id']} for r in reversed(rows)]

    def export_items(self, room_id=None):
        # 截止ID固定，导出不追逐不断到来的新消息。
        end = self.db.execute('SELECT COALESCE(MAX(id),0) FROM timeline').fetchone()[0]
        cursor = 0
        while cursor < end:
            rows = self.db.execute(
                'SELECT id,data FROM timeline WHERE id>? AND id<=? AND (? IS NULL OR room_id=?) '
                'ORDER BY id LIMIT 500', (cursor, end, room_id, room_id)).fetchall()
            if not rows:
                break
            for row in rows:
                cursor = row['id']
                yield {**json.loads(row['data']), 'id': row['id']}

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
