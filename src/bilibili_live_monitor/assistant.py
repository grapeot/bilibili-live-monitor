"""独立派生状态；不实例化采集 Store，不写入源数据库。"""
import json
import sqlite3
import time
import unicodedata
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pypinyin import Style, pinyin


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def pronunciation(name, override=None):
    if override is not None:
        return {'text': override, 'uncertain': False, 'override': True}
    parts, uncertain, has_han = [], False, False
    candidates, unknown = [], []
    for char in name:
        if not unicodedata.name(char, '').startswith(('CJK UNIFIED IDEOGRAPH', 'CJK COMPATIBILITY IDEOGRAPH')):
            parts.append(char)
            continue
        has_han = True
        readings = pinyin(char, style=Style.TONE, heteronym=True)[0]
        if readings == [char]:
            parts.append(char)
            uncertain = True
            unknown.append(char)
        elif len(readings) > 1:
            parts.append(f'{char}({" / ".join(readings)})')
            candidates.append({'character': char, 'readings': readings})
            uncertain = True
        else:
            parts.append(readings[0])
    return {'text': ' '.join(parts) if has_han else '', 'uncertain': uncertain, 'override': False,
            'candidates': candidates, 'unknown': unknown}


def observed_time(data):
    """资格比较使用接收/观测时间，不使用本地导入或 ASR 修订时间。"""
    try:
        stamp = datetime.fromisoformat(data.get('received_utc') or data.get('observed_utc') or data['at_utc'])
        return stamp.timestamp() if stamp.tzinfo else float('-inf')
    except (KeyError, TypeError, ValueError):
        return float('-inf')


GIFT_FOLLOWUP_RULE = 'fan_club_gift_v1'
MAX_STATUS_EVIDENCE = 9  # 至多3条资格witness，合计不超过工具的12条上限。


def fan_club_gift(item):
    """只认结构化原子送礼；同名盲盒结果、佩戴牌和文本均不能证明送灯牌。"""
    gift = item.get('gift')
    if (item.get('kind') != 'gift' or not isinstance(gift, dict)
            or gift.get('event_type') not in ('SEND_GIFT', 'SEND_GIFT_V2')
            or gift.get('gift_id') != '34001' or gift.get('gift_name') != '粉丝团灯牌'
            or gift.get('blind_gift') is not None
            or type(gift.get('num')) is not int or gift['num'] <= 0
            or item.get('identity_status') == 'unresolved'):
        return None
    uid, nested_uid = item.get('sender_uid'), gift.get('sender_uid')
    if uid and nested_uid and uid != nested_uid:
        return None
    uid = uid or nested_uid
    if not isinstance(uid, str) or not uid.isdigit() or int(uid) <= 0:
        return None
    return uid, gift['num']


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class EvidenceRef(Strict):
    id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)


class Action(Strict):
    room_id: str
    session_id: str
    card_id: str = Field(min_length=1, max_length=150, pattern=r'^[\w:.-]+$')
    kind: Literal['chat', 'song', 'medal']
    expected_version: int = Field(ge=0)
    status: Literal['pending', 'responded', 'dismissed', 'queued', 'sung', 'rejected', 'done']
    title: str = Field(min_length=1, max_length=300)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=12)
    recipient_uid: str | None = None
    song_origin: Literal['request', 'gift'] = 'request'


class Actions(Strict):
    actions: list[Action] = Field(max_length=30)


STATUSES = {'chat': {'pending', 'responded', 'dismissed'},
            'song': {'pending', 'queued', 'sung', 'rejected'},
            'medal': {'pending', 'done', 'dismissed'}}


class Conflict(ValueError):
    pass


class AssistantStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables & {'timeline', 'raw_events', 'segments', 'rooms'}:
            self.db.close()
            raise ValueError('助手状态路径指向采集数据库，拒绝写入')
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER UNIQUE,
                evidence_id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence(
                id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL, changed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cards(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT, before_data TEXT, after_data TEXT);
            CREATE TABLE IF NOT EXISTS overrides(name TEXT PRIMARY KEY, reading TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aliases(
                alias_key TEXT NOT NULL UNIQUE, uid TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(uid,alias_key));
            CREATE TABLE IF NOT EXISTS agent_runs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_calls(key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_exchanges(
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, data TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, encode(value)))

    def configure(self, room, session, since, source_session):
        stamp = datetime.fromisoformat(since)
        if stamp.tzinfo is None:
            raise ValueError('since 必须含时区')
        config = {'room_id': room, 'session_id': session, 'since': since,
                  'source_session': source_session}
        with self.db:
            if self.get('config') not in (None, config):
                raise Conflict('此数据库已固定另一场次/房间/起点')
            self.put('config', config)

    def cards(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM cards ORDER BY rowid')]

    def card(self, card_id):
        row = self.db.execute('SELECT data FROM cards WHERE id=?', (card_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_card(self, card):
        before = self.card(card['id'])
        self.db.execute('INSERT OR REPLACE INTO cards VALUES(?,?)', (card['id'], encode(card)))
        self.db.execute('INSERT INTO history(card_id,before_data,after_data) VALUES(?,?,?)',
                        (card['id'], encode(before), encode(card)))

    def evidence(self, eid):
        row = self.db.execute('SELECT * FROM evidence WHERE id=?', (eid,)).fetchone()
        return {'id': row['id'], 'version': row['version'], 'data': json.loads(row['data'])} if row else None

    def ingest(self, rows):
        """游标涵盖忽略的上游事件；被接受的每个修订保留不可变副本。"""
        config = self.get('config')
        if not config:
            raise ValueError('尚未固定场次')
        since = datetime.fromisoformat(config['since']).timestamp()
        with self.db:
            generations = self.get('source_generations', {})
            for row in rows:
                if row['id'] <= self.get('cursor', 0):
                    continue
                self.put('cursor', row['id'])
                if row['kind'] != 'item':
                    continue
                item = json.loads(row['data'])
                if item.get('room_id') != config['room_id']:
                    continue
                try:
                    at = datetime.fromisoformat(item.get('received_utc') or item['at_utc']).timestamp()
                except (KeyError, TypeError, ValueError):
                    continue
                if item.get('kind') not in ('danmaku', 'transcript', 'entry', 'gift', 'like'):
                    continue
                generation = item.get('session_id')
                if not isinstance(generation, str) or not generation:
                    continue  # startup-gap 等无来源管理事件不能证明 generation 切换。
                first = generations.setdefault(generation, {'id': row['id'], 'at': at})
                first['at'] = min(first['at'], at)
                if at < since or generation != config['source_session']:
                    continue
                eid = str(item['id'])
                old = self.evidence(eid)
                if old and old['data'] == item:
                    continue
                version = old['version'] + 1 if old else 1
                self.db.execute('INSERT INTO observations(source_id,evidence_id,version,data) VALUES(?,?,?,?)',
                                (row['id'], eid, version, encode(item)))
                self.db.execute('INSERT OR REPLACE INTO evidence VALUES(?,?,?,?)',
                                (eid, version, encode(item), time.time()))
                if item['kind'] == 'danmaku' and not self.card('chat:'+eid):
                    self.save_card({'id': 'chat:'+eid, 'kind': 'chat', 'status': 'pending',
                                    'version': 1, 'manual': False, 'title': item.get('text', '')[:300],
                                    'author': item.get('author') or '', 'uid': item.get('sender_uid'),
                                    'evidence': [{'id': eid, 'version': version}]})
            self.put('source_generations', generations)
            anchor = generations.get(config['source_session'])
            # 已知旧 generation 的迟到事件仍是旧来源；新来源须同时晚于固定来源的首条序号和观测时间。
            self.put('source_mismatch', bool(anchor and any(
                g != config['source_session'] and first['id'] > anchor['id']
                and first['at'] >= max(since, anchor['at']) for g, first in generations.items())))

    def derive_medals(self, snapshot=None):
        config = self.get('config')
        if not config:
            return
        # 资格只由本场目标礼物触发；其它事件只补充同UID身份与guard证据。
        users, gifts = {}, {}
        for row in self.db.execute('SELECT id,version,data FROM evidence ORDER BY changed'):
            item = json.loads(row['data'])
            at = observed_time(item)
            if (item.get('room_id') != config['room_id'] or item.get('session_id') != config['source_session']
                    or at < datetime.fromisoformat(config['since']).timestamp()):
                continue
            target = fan_club_gift(item)
            if target:
                gift_uid, num = target
                gifts.setdefault(gift_uid, []).append({
                    'id': row['id'], 'version': row['version'], 'num': num,
                    'at': item.get('received_utc') or item['at_utc'], 'epoch': at})
            uid = target[0] if target else item.get('sender_uid')
            if not uid:
                continue
            u = users.setdefault(uid, {'author': '', 'guard': None, 'refs': {},
                                      'guard_at': float('-inf'),
                                      'identity_at': float('-inf')})
            if type(item.get('guard_level')) is int and at >= u['guard_at']:
                u['guard'] = item['guard_level']
                u['guard_at'] = at
                u['refs']['guard'] = {'id': row['id'], 'version': row['version']}
            if at >= u['identity_at']:
                u['author'] = item.get('author') or u['author']
                u['identity_at'] = at
                u['refs']['identity'] = {'id': row['id'], 'version': row['version']}
        completed = {c.get('uid') for c in self.cards()
                     if c['kind'] == 'song' and c['status'] in ('sung', 'rejected')}
        members = {str(m.get('sender_uid')) for m in (snapshot or {}).get('members', [])}
        with self.db:
            # 不把旧佩戴牌卡上的AI结论迁移到实际礼物跟进；原卡和变更历史仍可审计。
            for old in self.cards():
                if old['kind'] == 'medal' and old.get('qualification_rule') != GIFT_FOLLOWUP_RULE and old.get('eligible'):
                    self.save_card({**old, 'eligible': False, 'version': old['version']+1})
            for uid, u in users.items():
                received = sorted(gifts.get(uid, []), key=lambda g: (g['epoch'], g['id']))
                guard = u['guard']
                snapshot_applies = (snapshot and observed_time(snapshot) > u['guard_at']
                                    and (uid in members or snapshot.get('complete')))
                if snapshot_applies:
                    if uid in members:
                        guard = 3
                    elif snapshot.get('complete'):
                        guard = 0
                eligible = bool(received) and guard == 0 and uid not in completed
                cid = 'medal:gift:'+uid
                old = self.card(cid)
                if not old and not eligible:
                    continue
                card = old or {'id': cid, 'kind': 'medal', 'status': 'pending', 'manual': False,
                               'version': 0, 'title': '粉丝团灯牌送礼跟进', 'uid': uid}
                gift_refs = [{'id': g['id'], 'version': g['version']} for g in received]
                witness = gift_refs[:1]
                for ref in u['refs'].values():
                    if ref not in witness:
                        witness.append(ref)
                gift_ids = {r['id'] for r in gift_refs}
                status_refs = [r for r in card.get('status_evidence', [])
                               if r not in witness and r['id'] not in gift_ids][-MAX_STATUS_EVIDENCE:]
                refs = witness + [r for r in status_refs if r not in witness]
                previous_refs = card.get('evidence', [])
                ordered_refs = []
                for ref in previous_refs + refs:
                    if ref in refs and ref not in ordered_refs:
                        ordered_refs.append(ref)
                refs = ordered_refs
                next_card = {**card, 'author': u['author'], 'eligible': eligible, 'evidence': refs,
                             'qualification_evidence': witness, 'status_evidence': status_refs,
                             'qualification_rule': GIFT_FOLLOWUP_RULE, 'gift_evidence': gift_refs,
                             'gift_quantity': sum(g['num'] for g in received),
                             'first_gift_at': received[0]['at'] if received else None,
                             'last_gift_at': received[-1]['at'] if received else None}
                next_card.pop('guard_observation', None)
                if snapshot_applies:
                    next_card['guard_observation'] = {
                        **{k: snapshot.get(k) for k in ('id', 'source', 'observed_utc', 'complete', 'reported_total')},
                        'uid': uid, 'listed': uid in members}
                if next_card != old:
                    next_card['version'] += 1
                    self.save_card(next_card)

    def manual(self, session, cid, version, status=None, undo=False):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if session != (self.get('config') or {}).get('session_id'):
                raise ValueError('场次不匹配')
            old = self.card(cid)
            if not old or old['version'] != version:
                raise Conflict('卡片已经更新，请刷新后重试')
            if undo:
                row = self.db.execute('SELECT before_data FROM history WHERE card_id=? ORDER BY seq DESC LIMIT 1',
                                      (cid,)).fetchone()
                previous = json.loads(row[0]) if row else None
                if previous is None:
                    raise ValueError('没有可撤销的状态')
                status = previous['status']
            if status not in STATUSES[old['kind']]:
                raise ValueError('此卡片不支持该状态')
            card = {**old, 'status': status, 'manual': True, 'version': version+1}
            self.save_card(card)
        return card

    def apply(self, payload, target, offered, *, commit=True, advance_cursor=True):
        actions = Actions.model_validate(payload).actions
        config = self.get('config')
        with self.db if commit else nullcontext():
            if commit:
                self.db.execute('BEGIN IMMEDIATE')
            for action in actions:
                if action.room_id != config['room_id'] or action.session_id != config['session_id']:
                    raise ValueError('AI 场次/房间不匹配')
                if action.status not in STATUSES[action.kind]:
                    raise ValueError('AI 状态不匹配')
                old = self.card(action.card_id)
                if (old and (old['version'] != action.expected_version or old.get('manual'))
                        or not old and action.expected_version != 0):
                    raise Conflict('AI 旧版本或人工保护')
                if old and (action.card_id not in offered['cards'] or old['kind'] != action.kind):
                    raise ValueError('AI 不能操作上下文外卡片')
                if not old and (action.kind != 'song' or not action.card_id.startswith('song:')):
                    raise ValueError('AI 只能新建点歌项')
                refs = [ref.model_dump() for ref in action.evidence]
                items = []
                for ref in refs:
                    ev = self.evidence(ref['id'])
                    if not ev or ev['version'] != ref['version'] or ref not in offered['evidence']:
                        raise Conflict('AI 证据不存在、过期或不在上下文')
                    items.append(ev['data'])
                if action.kind == 'medal':
                    witness = old['qualification_evidence']
                    required = {r['id'] for r in witness}
                    if not required.issubset({r['id'] for r in refs}):
                        raise ValueError('灯牌状态必须保留资格witness')
                    gift_ids = {r['id'] for r in old['gift_evidence']}
                    status_refs = []
                    for ref in old.get('status_evidence', []) + refs:
                        if ref not in witness and ref['id'] not in gift_ids:
                            if ref in status_refs:
                                status_refs.remove(ref)
                            status_refs.append(ref)
                    status_refs = status_refs[-MAX_STATUS_EVIDENCE:]
                    self.save_card({**old, 'status': action.status, 'version': action.expected_version+1,
                                    'evidence': witness + status_refs, 'status_evidence': status_refs})
                    continue  # 只改待办状态，不接收 AI 的昵称、标题、资格或榜单改写。
                origin = next((i for i in items if i['kind'] == 'danmaku'), None)
                if not old and action.song_origin == 'gift':
                    uid = action.recipient_uid
                    identities = [i for i in items if i.get('sender_uid') == uid and uid]
                    names = {i.get('author') for i in identities if i.get('author')}
                    names.update(r[0] for r in self.db.execute('SELECT alias_key FROM aliases WHERE uid=?', (uid,)))
                    voices = [i for i in items if i['kind'] == 'transcript' and action.title in i.get('text', '')
                              and any(len(name) >= 2 and name.casefold() in i.get('text', '').casefold()
                                      for name in names)]
                    if not identities or not voices:
                        raise ValueError('主动赠歌需要明确UID身份和歌曲/称呼语音证据')
                    origin = {**voices[0], 'sender_uid': uid, 'author': identities[0].get('author', '')}
                elif not old and not origin:
                    raise ValueError('点歌项必须有弹幕来源')
                elif not old and action.recipient_uid and origin.get('sender_uid') != action.recipient_uid:
                    raise ValueError('点歌接收者与来源身份不匹配')
                origin_id = (old or {}).get('origin_id') or (str(origin['id']) if origin else None)
                if old and action.kind == 'chat' and action.card_id.removeprefix('chat:') not in {
                        r['id'] for r in refs}:
                    raise ValueError('回应必须保留原弹幕证据')
                if old and action.kind == 'song' and origin_id not in {r['id'] for r in refs}:
                    raise ValueError('点歌状态必须保留来源证据')
                if not old and any(c['kind'] == 'song' and c.get('origin_id') == origin_id for c in self.cards()):
                    raise Conflict('同一弹幕点歌已存在')
                origin = origin or {}
                self.save_card({**(old or {}), 'id': action.card_id, 'kind': action.kind,
                                'title': action.title, 'status': action.status,
                                'version': action.expected_version+1, 'manual': False, 'evidence': refs,
                                'origin_id': origin_id,
                                'song_origin': (old or {}).get('song_origin', action.song_origin),
                                'uid': (old or {}).get('uid') or origin.get('sender_uid'),
                                'author': (old or {}).get('author') or origin.get('author') or ''})
            if advance_cursor:
                self.put('ai_cursor', target)
                self.put('last_analysis', time.time())
            self.put('last_error', None)

    def state(self):
        readings = dict(self.db.execute('SELECT name,reading FROM overrides'))
        cards = [{**c, 'pronunciation': pronunciation(c.get('author', ''), readings.get(c.get('author', '')))}
                 for c in self.cards()]
        refs = {(r['id'], r['version']) for c in cards for r in c['evidence'] + c.get('gift_evidence', [])}
        evidence = []
        for eid, version in refs:
            row = self.db.execute('SELECT data FROM observations WHERE evidence_id=? AND version=? LIMIT 1',
                                  (eid, version)).fetchone()
            if row:
                latest = self.evidence(eid)
                evidence.append({'id': eid, 'version': version, 'data': json.loads(row[0]),
                                 'latest_version': latest['version']})
        pending = self.db.execute('SELECT count(*) FROM observations WHERE seq>?',
                                  (self.get('ai_cursor', 0),)).fetchone()[0]
        latest = self.db.execute('SELECT max(changed) FROM evidence').fetchone()[0]
        return {'config': self.get('config'), 'cards': cards,
                'agent': {**self.get('agent_metrics', {}), 'status': self.get('agent_status', 'sleeping'),
                          'aliases': [json.loads(r[0]) for r in self.db.execute('SELECT data FROM aliases')],
                          'trace': [json.loads(r[0]) for r in self.db.execute(
                              'SELECT data FROM agent_audit ORDER BY id DESC LIMIT 20')]},
                'evidence': evidence,
                'worker': {'pending_observations': pending, 'last_observation': latest,
                           **{key: self.get(key) for key in
                              ('status', 'last_error', 'cursor', 'ai_cursor', 'calls', 'ended',
                               'last_attempt', 'last_analysis')}}}
