"""房间级持久工具循环：只操作助手库，无SQL/执行/网络工具。"""
import hashlib
import json
import time
import uuid
from copy import deepcopy
from datetime import datetime

from pydantic import Field

from .assistant import Actions, Conflict, EvidenceRef, Strict, encode, observed_time


class Scope(Strict):
    room_id: str
    session_id: str


class ReadState(Scope):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=80)


class ViewerEvents(Scope):
    uid: str = Field(min_length=1, max_length=40, pattern=r'^\d+$')
    since: str | None = None
    until: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class SearchTranscripts(Scope):
    keyword: str = Field(min_length=1, max_length=100)
    since: str | None = None
    until: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class ReadEvidence(Scope):
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=12)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=2000, ge=1, le=8000)


class RecordAlias(Scope):
    uid: str = Field(min_length=1, max_length=40, pattern=r'^\d+$')
    alias: str = Field(min_length=2, max_length=80)
    expected_version: int = Field(ge=0)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=12)
    reason: str = Field(min_length=1, max_length=300)


class Finish(Scope):
    reason: str = Field(min_length=1, max_length=300)


MODELS = {'read_state': ReadState, 'viewer_events': ViewerEvents, 'search_transcripts': SearchTranscripts,
          'read_evidence': ReadEvidence, 'record_alias': RecordAlias,
          'update_assistant': Actions, 'finish_analysis': Finish}
DESCRIPTIONS = {
    'read_state': '分页读取当前待办与持久别名，取得卡片版本。',
    'viewer_events': '按UID查询本场人物事件；可指定时间范围，返回覆盖信息和证据版本。',
    'search_transcripts': '按关键词和可选时间范围检索本场转写，不提供任意SQL。',
    'read_evidence': '按ID/版本读取证据原文，长文本用offset/max_chars分页。',
    'record_alias': '记录可查证的原文称呼关联，UID与别名证据都必需；冲突不合并。',
    'update_assistant': '有证据地原子更新派生待办；主动赠歌用recipient_uid和song_origin=gift。',
    'finish_analysis': '确认本批观察分析完成；之后休眠直到新事件到达。',
}
TOOLS = [{'type': 'function', 'function': {'name': name, 'description': DESCRIPTIONS[name],
          'parameters': model.model_json_schema()}} for name, model in MODELS.items()]


def compact_card(card):
    return {**{k: v for k, v in card.items() if k != 'gift_evidence'},
            **({'gift_receipt_count': len(card['gift_evidence'])} if 'gift_evidence' in card else {})}


class RoomAgent:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def metric(self, **changes):
        value = {**self.store.get('agent_metrics', {}), **changes}
        self.store.put('agent_metrics', value)

    def bump(self, name, value=1):
        self.metric(**{name: self.store.get('agent_metrics', {}).get(name, 0)+value})

    def active(self):
        rid = self.store.get('active_agent_run')
        row = self.store.db.execute('SELECT data FROM agent_runs WHERE id=?', (rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, run):
        self.store.db.execute('INSERT OR REPLACE INTO agent_runs VALUES(?,?)', (run['id'], encode(run)))

    def start(self, context, target, offered):
        # 初始现场只是有界预览；原文通过查询工具取回，不在对话中复制全库。
        preview = []
        truncated = 0
        for ev in context['evidence']:
            data = ev['data']
            text = data.get('text', '')
            truncated += len(text) > 600
            preview.append({'id': ev['id'], 'version': ev['version'], 'kind': data['kind'],
                            'uid': data.get('sender_uid'), 'author': data.get('author'),
                            'at': data.get('received_utc') or data.get('at_utc'), 'text': text[:600],
                            'truncated': len(text) > 600})
        run = {'id': uuid.uuid4().hex, 'target': target, 'turns': 0, 'offered': offered,
               'input': {'scope': context['scope'], 'incremental_ids': context['incremental_ids'],
                         'events': preview, 'coverage': {'preview_truncated': truncated}},
               'created': time.time(), 'status': 'active'}
        with self.store.db:
            self.store.put('active_agent_run', run['id'])
            self.store.put('agent_status', 'working')
            self.save(run)
            self.store.db.execute('INSERT INTO agent_exchanges(run_id,at,data) VALUES(?,?,?)',
                                  (run['id'], run['created'], encode([{'role': 'user', 'content': encode(run['input'])}])))
        return run

    def durable(self):
        cards = [compact_card(c) for c in self.store.cards()
                 if c['status'] in ('pending', 'queued') and (c['kind'] != 'medal' or c.get('eligible'))]
        aliases = [json.loads(r[0]) for r in self.store.db.execute('SELECT data FROM aliases ORDER BY alias_key')]
        refs = {(r['id'], r['version']) for c in cards for r in c['evidence']}
        refs.update((r['id'], r['version']) for a in aliases for r in a['evidence'])
        return {'scope': self.store.get('config'), 'unfinished': cards, 'aliases': aliases,
                'necessary_evidence': [{'id': eid, 'version': version} for eid, version in sorted(refs)]}

    def messages(self, run, prompt):
        base = [{'role': 'system', 'content': prompt},
                {'role': 'system', 'content': '持久工作状态（不是临时对话）：'+encode(self.durable())}]
        rows = self.store.db.execute('SELECT id,run_id,at,data FROM agent_exchanges ORDER BY id').fetchall()
        current_input = next((r for r in rows if r['run_id'] == run['id']
                              and json.loads(r['data'])[0]['role'] == 'user'), None)
        input_unit = (current_input['id'], run['created'], [{'role': 'user', 'content': encode(run['input'])}])
        units = [(r['id'], r['at'], json.loads(r['data'])) for r in rows
                 if not (r['run_id'] == run['id'] and json.loads(r['data'])[0]['role'] == 'user')]
        cap = min(self.config.max_bytes, self.config.context_tokens)

        def assemble(selected):
            ordered = sorted([*selected, input_unit], key=lambda u: u[0])
            return base + [m for _, _, unit in ordered for m in unit]

        def size(selected):
            messages = assemble(selected)
            # UTF8序列化长度仅作保守预算代理，不声称是模型精确token计数。
            return len(encode({'model': self.config.model, 'messages': messages, 'tools': TOOLS,
                               'stream': False, 'think': False,
                               'options': {'num_predict': self.config.max_tokens}}).encode())

        actual = self.store.get('agent_metrics', {}).get('last_prompt_eval_count')
        trigger = size(units) > cap or (actual is not None and actual > self.config.context_tokens)
        removed, dense = [], 0
        if trigger:
            cutoff = time.time()-self.config.recent_seconds
            removed = [u for u in units if u[1] < cutoff]
            units = [u for u in units if u[1] >= cutoff]
            while units and size(units) > cap:
                removed.append(units.pop(0))
                dense += 1
        with self.store.db:
            if removed:
                self.store.db.executemany('DELETE FROM agent_exchanges WHERE id=?', [(u[0],) for u in removed])
                self.bump('compaction_count')
                self.bump('dropped_exchanges', len(removed))
                self.bump('dense_window_drops', dense)
            self.metric(input_bytes=size(units), byte_limit=cap, context_token_limit=self.config.context_tokens,
                        recent_seconds=self.config.recent_seconds, retained_exchanges=len(units))
        if size(units) > cap:
            with self.store.db:
                self.bump('durable_overflow_count')
            raise ValueError('持久工作状态超过上下文预算；未丢弃待办或别名')
        return assemble(units)

    def check_scope(self, args):
        scope = self.store.get('config')
        if args.room_id != scope['room_id'] or args.session_id != scope['session_id']:
            raise ValueError('工具房间/场次不匹配')

    def offer(self, run, evidence=(), cards=()):
        for ev in evidence:
            ref = {'id': ev['id'], 'version': ev['version']}
            if ref not in run['offered']['evidence']:
                run['offered']['evidence'].append(ref)
        for c in cards:
            if c['id'] not in run['offered']['cards']:
                run['offered']['cards'].append(c['id'])

    def refs(self, run, refs):
        evidence = []
        for ref in refs:
            value = ref.model_dump()
            ev = self.store.evidence(ref.id)
            if not ev or ev['version'] != ref.version or value not in run['offered']['evidence']:
                raise Conflict('证据未提供、已修订或不存在')
            evidence.append(ev)
        return evidence

    def events(self, args, *, transcript=False):
        scope = self.store.get('config')
        low = datetime.fromisoformat(scope['since']).timestamp()
        high = float('inf')
        for attr in ('since', 'until'):
            value = getattr(args, attr)
            if value:
                dt = datetime.fromisoformat(value)
                if dt.tzinfo is None:
                    raise ValueError('工具时间范围必须有时区')
                if attr == 'since':
                    low = max(low, dt.timestamp())
                else:
                    high = dt.timestamp()
        if low > high:
            raise ValueError('工具时间范围倒置')
        # SQL固定模板；不接受任意SQL或通配符表达式。
        if transcript:
            cursor = self.store.db.execute("SELECT id,version,data FROM evidence WHERE "
                "json_extract(data,'$.kind')='transcript' AND instr(lower(json_extract(data,'$.text')),lower(?))>0",
                (args.keyword,))
        else:
            cursor = self.store.db.execute("SELECT id,version,data FROM evidence WHERE "
                "json_extract(data,'$.sender_uid')=?", (args.uid,))
        found = []
        for row in cursor:
            data = json.loads(row['data'])
            if (data.get('room_id') == scope['room_id'] and data.get('session_id') == scope['source_session']
                    and low <= observed_time(data) <= high):
                found.append({'id': row['id'], 'version': row['version'], 'data': data})
        found.sort(key=lambda e: (observed_time(e['data']), e['id']), reverse=True)
        return found

    def preview(self, ev, offset=0, chars=600):
        data = ev['data']
        text = data.get('text', '')
        return {'id': ev['id'], 'version': ev['version'], 'data': {
            **{k: v for k, v in data.items() if k not in ('text', 'gift')},
            'text': text[offset:offset+chars],
            **({'gift': data['gift']} if data.get('gift') else {})},
            'text_total_chars': len(text), 'offset': offset,
            'truncated': offset > 0 or len(text) > offset+chars}

    def dispatch(self, name, raw, run):
        if name not in MODELS:
            raise ValueError('工具不在白名单')
        args = MODELS[name].model_validate(raw)
        if name != 'update_assistant':
            self.check_scope(args)
        if name == 'read_state':
            all_cards = self.store.cards()
            cards = all_cards[args.offset:args.offset+args.limit]
            evidence = [self.store.evidence(r['id']) for c in cards for r in c['evidence']]
            self.offer(run, [e for e in evidence if e], cards)
            return {'cards': [compact_card(c) for c in cards], 'aliases': self.durable()['aliases'],
                    'total': len(all_cards), 'truncated': args.offset+len(cards) < len(all_cards)}
        if name in ('viewer_events', 'search_transcripts'):
            found = self.events(args, transcript=name == 'search_transcripts')
            selected = found[args.offset:args.offset+args.limit]
            self.offer(run, selected)
            return {'events': [self.preview(e) for e in selected], 'matched': len(found),
                    'returned': len(selected), 'offset': args.offset,
                    'truncated': args.offset+len(selected) < len(found)}
        if name == 'read_evidence':
            # 允许查阅不可变旧修订；写入仍由refs/apply要求当前版本。
            evidence = []
            scope = self.store.get('config')
            for ref in args.evidence:
                ev = self.store.evidence(ref.id)
                if ev and ev['version'] != ref.version:
                    row = self.store.db.execute('SELECT data FROM observations WHERE evidence_id=? AND version=?',
                                                (ref.id, ref.version)).fetchone()
                    ev = {'id': ref.id, 'version': ref.version, 'data': json.loads(row[0])} if row else None
                if (not ev or ev['data'].get('room_id') != scope['room_id']
                        or ev['data'].get('session_id') != scope['source_session']):
                    raise Conflict('原始证据版本/范围不匹配')
                evidence.append(ev)
            self.offer(run, evidence)
            return {'events': [self.preview(e, args.offset, args.max_chars) for e in evidence]}
        if name == 'record_alias':
            evidence = self.refs(run, args.evidence)
            alias = args.alias.strip()
            if (len(alias) < 2 or not any(e['data'].get('sender_uid') == args.uid for e in evidence)
                    or not any(alias.casefold() in e['data'].get('text', '').casefold() for e in evidence)):
                raise ValueError('别名缺少明确UID或原文证据')
            key = alias.casefold()
            row = self.store.db.execute('SELECT uid,data FROM aliases WHERE alias_key=?', (key,)).fetchone()
            old = json.loads(row['data']) if row else None
            if (old and (row['uid'] != args.uid or old['version'] != args.expected_version)
                    or not old and args.expected_version != 0):
                raise Conflict('别名归属/版本冲突，不自动合并')
            data = {'uid': args.uid, 'alias': alias, 'version': args.expected_version+1,
                    'evidence': [r.model_dump() for r in args.evidence], 'reason': args.reason}
            self.store.db.execute('INSERT OR REPLACE INTO aliases VALUES(?,?,?)', (key, args.uid, encode(data)))
            return data
        if name == 'update_assistant':
            self.store.apply(raw, run['target'], run['offered'], commit=False, advance_cursor=False)
            return {'updated': [a.card_id for a in args.actions]}
        run['status'] = 'finished'
        self.store.put('ai_cursor', run['target'])
        self.store.put('last_analysis', time.time())
        self.store.put('active_agent_run', None)
        return {'finished': True}

    def exchange(self, run, response):
        """仅成对持久化；工具写、幂等回执、对话和审计同事务提交。"""
        message = response.get('message', {})
        calls = message.get('tool_calls', [])
        if len(calls) > self.config.max_tools_per_turn:
            raise ValueError('本轮工具数超过上限')
        if not calls:
            if not message.get('content'):
                raise ValueError('模型未返回工具或完成说明')
            calls = [{'function': {'name': 'finish_analysis', 'arguments': {
                **{k: self.store.get('config')[k] for k in ('room_id', 'session_id')},
                'reason': message['content'][:300]}}}]
        assistant = {'role': 'assistant', 'content': message.get('content', ''), 'tool_calls': calls}
        messages = [assistant]
        with self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            for i, call in enumerate(calls):
                fn = call.get('function', {})
                name = fn.get('name', '')
                raw = fn.get('arguments', {})
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except ValueError:
                        raw = {}
                key = hashlib.sha256(encode([run['id'], name, raw]).encode()).hexdigest()
                row = self.store.db.execute('SELECT data FROM agent_calls WHERE key=?', (key,)).fetchone()
                cached = (row is not None and name in ('record_alias', 'update_assistant', 'finish_analysis')
                          and 'error' not in json.loads(row[0]))
                if cached:
                    result = json.loads(row[0])
                else:
                    self.store.db.execute('SAVEPOINT tool_action')
                    offered_before = deepcopy(run['offered'])
                    try:
                        if run['status'] == 'finished':
                            raise ValueError('finish之后不再执行工具')
                        result = self.dispatch(name, raw, run)
                        self.store.db.execute('RELEASE tool_action')
                    except Exception as exc:
                        self.store.db.execute('ROLLBACK TO tool_action')
                        self.store.db.execute('RELEASE tool_action')
                        result = {'error': type(exc).__name__, 'hint': '请核对scope、证据版本和人工锁，再查询当前状态'}
                        run['offered'] = offered_before
                    if result.get('truncated') or any(e.get('truncated') for e in result.get('events', [])):
                        self.bump('truncated_query_results')
                    text = encode(result)
                    if len(text.encode()) > self.config.tool_result_bytes:
                        # 结果在发送前就压为显式覆盖提示；完整原文仍可按ID分页查询。
                        result = {'truncated': True, 'reason': 'tool_result_bytes',
                                  'hint': '缩小limit或max_chars后重查', 'result_bytes': len(text.encode())}
                        if name in ('read_state', 'viewer_events', 'search_transcripts', 'read_evidence'):
                            run['offered'] = offered_before
                        self.bump('truncated_tool_results')
                    self.store.db.execute('INSERT OR REPLACE INTO agent_calls VALUES(?,?)', (key, encode(result)))
                msg = {'role': 'tool', 'tool_name': name, 'content': encode(result)}
                if call.get('id'):
                    msg['tool_call_id'] = call['id']
                messages.append(msg)
                audit = {'at': time.time(), 'run_id': run['id'], 'turn': run['turns']+1, 'tool': name,
                         'cached': cached, 'error': result.get('error'),
                         'reason': str(raw.get('reason', DESCRIPTIONS.get(name, '未知工具'))
                                       if isinstance(raw, dict) else '非法参数结构')[:300],
                         'evidence': raw.get('evidence', []) if isinstance(raw, dict) else []}
                if name == 'update_assistant' and isinstance(raw, dict):
                    audit['evidence'] = [r for a in raw.get('actions', []) for r in a.get('evidence', [])]
                    audit['reason'] = '; '.join(f"{a.get('card_id')}→{a.get('status')}" for a in raw.get('actions', []))[:300]
                self.store.db.execute('INSERT INTO agent_audit(at,data) VALUES(?,?)', (audit['at'], encode(audit)))
            self.store.db.execute('INSERT INTO agent_exchanges(run_id,at,data) VALUES(?,?,?)',
                                  (run['id'], time.time(), encode(messages)))
            run['turns'] += 1
            self.save(run)
            usage = response.get('prompt_eval_count')
            self.metric(last_prompt_eval_count=usage if type(usage) is int else None)
            self.store.put('agent_status', 'sleeping' if run['status'] == 'finished' else 'working')
            self.store.put('last_error', None)
        return run['status'] == 'finished'
