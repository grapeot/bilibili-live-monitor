"""房间级持久工具循环：只操作助手库，无SQL/执行/网络工具。"""
import hashlib
import json
import math
import time
import uuid
from copy import deepcopy
from datetime import datetime

from pydantic import Field

from .assistant import Actions, Conflict, EvidenceRef, Strict, encode, observed_time
from .assistant_aliases import aliases, relationship


class Scope(Strict):
    room_id: str
    session_id: str


class ReadState(Scope):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=80)
    state_filter: str = Field(default='all', pattern='^(all|unfinished)$')
    uid: str | None = None


class ReadAliases(Scope):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=30, ge=1, le=50)
    uid: str | None = None
    keyword: str = Field(default='', max_length=100)
    status: str = Field(default='all', pattern='^(all|confirmed|pending_review|unconfirmed)$')


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
    identity_evidence: list[EvidenceRef] | None = Field(default=None, min_length=1, max_length=12)
    relation_evidence: list[EvidenceRef] | None = Field(default=None, min_length=1, max_length=12)
    quoted_relation: str = Field(default='', max_length=600)


class Finish(Scope):
    reason: str = Field(min_length=1, max_length=300)


MODELS = {'read_state': ReadState, 'read_aliases': ReadAliases,
          'viewer_events': ViewerEvents, 'search_transcripts': SearchTranscripts,
          'read_evidence': ReadEvidence, 'record_alias': RecordAlias,
          'update_assistant': Actions, 'finish_analysis': Finish}
DESCRIPTIONS = {
    'read_state': '分页读取卡片版本；state_filter=unfinished查看未完成项，别名单独用read_aliases。',
    'read_aliases': '按UID、关键词或关系状态分页读取别名，包括待复核/未确认关系。',
    'viewer_events': '按UID查询本场人物事件；可指定时间范围，返回覆盖信息和证据版本。',
    'search_transcripts': '按关键词和可选时间范围检索本场转写，不提供任意SQL。',
    'read_evidence': '按ID/版本读取证据原文，长文本用offset/max_chars分页。',
    'record_alias': '区分identity_evidence和relation_evidence，并摘录quoted_relation。只接受明确自称或已知昵称的肯定关联；冲突不合并。',
    'update_assistant': '有证据地原子更新派生待办；主动赠歌用recipient_uid和song_origin=gift。',
    'finish_analysis': '确认本批观察分析完成；之后休眠直到新事件到达。',
}
TOOLS = [{'type': 'function', 'function': {'name': name, 'description': DESCRIPTIONS[name],
          'parameters': model.model_json_schema()}} for name, model in MODELS.items()]


def compact_card(card):
    return {**{k: v for k, v in card.items() if k != 'gift_evidence'},
            **({'gift_receipt_count': len(card['gift_evidence'])} if 'gift_evidence' in card else {})}


class ContextCapacityError(ValueError):
    pass


def token_estimate(payload):
    """本地启发式，不是DeepSeek tokenizer：ASCII按3.5字符/token，其余按1.3。"""
    text = encode(payload)
    ascii_chars = sum(ord(c) < 128 for c in text)
    return math.ceil(ascii_chars/3.5 + (len(text)-ascii_chars)*1.3)


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

    def durable(self, run=None):
        cards = [compact_card(c) for c in self.store.cards()
                 if c['status'] in ('pending', 'queued') and (c['kind'] != 'medal' or c.get('eligible'))]
        ids = set((run or {}).get('input', {}).get('incremental_ids', []))
        cards.sort(key=lambda c: (not any(r['id'] in ids for r in c['evidence']), c['kind'] == 'chat'))
        page = cards[:20]
        all_aliases = aliases(self.store, include_candidates=True)
        uids = {c.get('uid') for c in page}
        all_aliases.sort(key=lambda a: a['uid'] not in uids)
        alias_page = all_aliases[:10]
        refs = {(r['id'], r['version']) for c in page for r in c['evidence']}
        refs.update((r['id'], r['version']) for a in alias_page for r in a['evidence'])
        return {'scope': self.store.get('config'), 'unfinished': page, 'aliases': alias_page,
                'work_index': {'total': len(cards), 'shown': len(page), 'next_offset': 0 if cards else None,
                               'tool': 'read_state', 'state_filter': 'unfinished',
                               'note': '此处按当前相关性选取；完整列表从offset=0查询'},
                'alias_index': {'total': len(all_aliases), 'shown': len(alias_page), 'tool': 'read_aliases',
                                'note': '首页优先当前观众；完整列表用独立offset分页'},
                'necessary_evidence': [{'id': eid, 'version': version} for eid, version in sorted(refs)]}

    def messages(self, run, prompt):
        base = [{'role': 'system', 'content': prompt},
                {'role': 'system', 'content': '持久工作状态索引（全量留在DB，可分页查询）：'+encode(self.durable(run))}]
        rows = self.store.db.execute('SELECT id,run_id,at,data FROM agent_exchanges ORDER BY id').fetchall()
        current_input = next((r for r in rows if r['run_id'] == run['id']
                              and json.loads(r['data'])[0]['role'] == 'user'), None)
        input_unit = (current_input['id'], run['created'], [{'role': 'user', 'content': encode(run['input'])}])
        units = [(r['id'], r['at'], json.loads(r['data'])) for r in rows
                 if not (r['run_id'] == run['id'] and json.loads(r['data'])[0]['role'] == 'user')]

        def assemble(selected):
            ordered = sorted([*selected, input_unit], key=lambda u: u[0])
            return base + [m for _, _, unit in ordered for m in unit]

        def payload(selected):
            messages = assemble(selected)
            return {'model': self.config.model, 'messages': messages, 'tools': TOOLS,
                    'stream': False, 'think': False, 'options': {'num_predict': self.config.max_tokens}}

        ratio = self.store.get('agent_metrics', {}).get('estimator_calibration', 1.0)

        def measures(selected):
            data = payload(selected)
            base_tokens = token_estimate(data)
            return len(encode(data).encode()), math.ceil(base_tokens*ratio), base_tokens

        def exceeds(selected):
            byte_count, estimated, _ = measures(selected)
            return byte_count > self.config.max_bytes or estimated > self.config.context_tokens

        actual = self.store.get('agent_metrics', {}).get('last_prompt_eval_count')
        trigger = exceeds(units) or (actual is not None and actual > self.config.context_tokens)
        removed, dense = [], 0
        if trigger:
            cutoff = time.time()-self.config.recent_seconds
            removed = [u for u in units if u[1] < cutoff]
            units = [u for u in units if u[1] >= cutoff]
            while units and exceeds(units):
                removed.append(units.pop(0))
                dense += 1
        with self.store.db:
            if removed:
                self.store.db.executemany('DELETE FROM agent_exchanges WHERE id=?', [(u[0],) for u in removed])
                self.bump('compaction_count')
                self.bump('dropped_exchanges', len(removed))
                self.bump('dense_window_drops', dense)
            byte_count, estimated, base_tokens = measures(units)
            self.metric(input_bytes=byte_count, byte_limit=self.config.max_bytes,
                        estimated_input_tokens=estimated, last_input_base_tokens=base_tokens,
                        token_estimator='ascii/3.5 + nonascii*1.3; usage-calibrated, not DeepSeek tokenizer',
                        context_token_limit=self.config.context_tokens, estimator_calibration=ratio,
                        recent_seconds=self.config.recent_seconds, retained_exchanges=len(units))
        if exceeds(units):
            with self.store.db:
                self.bump('durable_overflow_count')
                self.metric(capacity_reason='byte_limit' if byte_count > self.config.max_bytes else 'token_estimate')
            raise ContextCapacityError('最小工作索引/当前输入超过上下文预算；数据仍在DB')
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
            if args.state_filter == 'unfinished':
                all_cards = [c for c in all_cards if c['status'] in ('pending', 'queued')
                             and (c['kind'] != 'medal' or c.get('eligible'))]
            if args.uid:
                all_cards = [c for c in all_cards if c.get('uid') == args.uid]
            cards = all_cards[args.offset:args.offset+args.limit]
            evidence = [self.store.evidence(r['id']) for c in cards for r in c['evidence']]
            self.offer(run, [e for e in evidence if e], cards)
            more = args.offset+len(cards) < len(all_cards)
            return {'cards': [compact_card(c) for c in cards], 'total': len(all_cards),
                    'offset': args.offset, 'next_offset': args.offset+len(cards) if more else None,
                    'truncated': more}
        if name == 'read_aliases':
            found = [a for a in aliases(self.store, args.uid, include_candidates=True)
                     if (args.status == 'all' or a['status'] == args.status)
                     and args.keyword.casefold() in a['alias'].casefold()]
            selected = found[args.offset:args.offset+args.limit]
            more = args.offset+len(selected) < len(found)
            return {'aliases': selected, 'total': len(found), 'offset': args.offset,
                    'next_offset': args.offset+len(selected) if more else None, 'truncated': more}
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
            identities = self.refs(run, args.identity_evidence) if args.identity_evidence else evidence
            relations = self.refs(run, args.relation_evidence) if args.relation_evidence else evidence
            alias = args.alias.strip()
            if (len(alias) < 2 or not any(e['data'].get('sender_uid') == args.uid for e in identities)
                    or not any(alias.casefold() in e['data'].get('text', '').casefold() for e in relations)):
                raise ValueError('别名缺少明确UID或原文证据')
            key = alias.casefold()
            row = self.store.db.execute('SELECT uid,data FROM aliases WHERE alias_key=?', (key,)).fetchone()
            old = json.loads(row['data']) if row else None
            if (old and (row['uid'] != args.uid or old['version'] != args.expected_version)
                    or not old and args.expected_version != 0):
                raise Conflict('别名归属/版本冲突，不自动合并')
            proof = relationship(self.store, args.uid, alias, identities, relations, args.quoted_relation)
            refs = list({(e['id'], e['version']): {'id': e['id'], 'version': e['version']}
                         for e in evidence+identities+relations}.values())
            data = {'uid': args.uid, 'alias': alias, 'version': args.expected_version+1,
                    'evidence': refs, 'reason': args.reason,
                    'identity_evidence': [{'id': e['id'], 'version': e['version']} for e in identities
                                          if e['data'].get('sender_uid') == args.uid],
                    'relation_evidence': [{'id': e['id'], 'version': e['version']} for e in relations],
                    'status': 'confirmed' if proof else 'unconfirmed', **(proof or {})}
            if not proof:
                data['quoted_relation'] = args.quoted_relation
                candidate_id = hashlib.sha256(encode(data).encode()).hexdigest()
                self.store.db.execute('INSERT OR REPLACE INTO alias_candidates VALUES(?,?,?)',
                                      (candidate_id, args.uid, encode(data)))
                return {**data, 'error': 'UnconfirmedRelation', 'hint': '原文未明确肯定关联该UID；请查证后重新确认'}
            if old:
                self.store.db.execute('INSERT INTO alias_history(alias_key,data) VALUES(?,?)', (key, encode(old)))
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

    def exchange(self, run, response, *, control_revision=None):
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
            self.store.require_enabled(control_revision)
            row = self.store.db.execute('SELECT data FROM agent_runs WHERE id=?', (run['id'],)).fetchone()
            if not row:
                raise Conflict('任务不存在')
            persisted = json.loads(row[0])
            run.update(persisted)
            if run['status'] == 'finished':
                return True
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
                        metadata = {k: result[k] for k in ('total', 'matched', 'offset') if k in result}
                        result = {**metadata, 'truncated': True, 'reason': 'tool_result_bytes',
                                  'next_offset': raw.get('offset', 0),
                                  'suggested_limit': max(1, raw.get('limit', 30)//2),
                                  'hint': '本页未返回条目；保持offset，缩小limit或max_chars后重查',
                                  'result_bytes': len(text.encode())}
                        if name in ('read_state', 'read_aliases', 'viewer_events', 'search_transcripts', 'read_evidence'):
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
            base = self.store.get('agent_metrics', {}).get('last_input_base_tokens')
            if type(usage) is int and usage > 0 and base:
                ratio = usage/base
                self.metric(observed_token_ratio=ratio, estimator_calibration=max(1., min(4., ratio*1.1)))
            self.store.put('agent_status', 'sleeping' if run['status'] == 'finished' else 'working')
            self.store.put('last_error', None)
        return run['status'] == 'finished'
