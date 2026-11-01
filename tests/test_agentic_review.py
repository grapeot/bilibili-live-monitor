"""318623f独立审阅R1–R4/N1–N2的离线反例；无网络或真实模型。"""
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from bilibili_live_monitor.assistant import AssistantStore, encode
from bilibili_live_monitor.assistant_worker import PROMPT, AssistantWorker, WorkerConfig

S = {'room_id': '123456', 'session_id': 'scene'}
T = '2030-01-01T00:00:00+00:00'


def event(n, text='你好', kind='danmaku', uid='111', author='ViewerA', eid=None):
    return {'id': n, 'kind': 'item', 'data': encode({
        'id': str(n if eid is None else eid), 'kind': kind, 'room_id': '123456',
        'session_id': 'generation', 'received_utc': T, 'at_utc': T, 'text': text,
        'sender_uid': uid, 'author': author, 'is_final': True})}


@pytest.fixture
def make(tmp_path):
    stores = []
    def setup(rows):
        store = AssistantStore(tmp_path/f'assistant-{len(stores)}.sqlite')
        stores.append(store)
        store.configure('123456', 'scene', T, 'generation')
        store.ingest(rows)
        worker = AssistantWorker(store, tmp_path/'unused-source.sqlite', 'http://synthetic',
                                 WorkerConfig(interval=0), runtime=AsyncMock())
        store.set_enabled(True)
        worker.ingest = lambda: None
        worker.gate = AsyncMock(return_value='live')
        return store, worker
    yield setup
    for store in stores:
        store.close()


def start(worker):
    context, target, offered = worker.context(enforce_limit=False)
    return worker.agent.start(context, target, offered)


def call(name, args):
    return {'message': {'tool_calls': [{'function': {'name': name, 'arguments': args}}]}}


def last(store):
    row = store.db.execute('SELECT data FROM agent_exchanges ORDER BY id DESC LIMIT 1').fetchone()
    return json.loads(json.loads(row[0])[-1]['content'])


def alias_args():
    return {**S, 'uid': '111', 'alias': '清风老师', 'expected_version': 0,
            'evidence': [{'id': '1', 'version': 1}, {'id': '2', 'version': 1}], 'reason': '称呼关联'}


def gift(uid, title, identity='1', voice='3'):
    return {**S, 'card_id': 'song:'+uid, 'kind': 'song', 'title': title, 'status': 'queued',
            'expected_version': 0, 'song_origin': 'gift', 'recipient_uid': uid,
            'evidence': [{'id': identity, 'version': 1}, {'id': voice, 'version': 1}]}


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [500, 2000])
async def test_r1_default_pending_backlog_starts_and_advances_without_deleting_cards(make, count):
    store, worker = make([event(i, '普通聊天内容'*8, uid=str(i), author='观众'+str(i))
                          for i in range(1, count+1)])
    worker.runtime.infer.return_value = call('finish_analysis', {**S, 'reason': '已检查本批'})
    await worker.tick()
    assert store.get('calls') == 1 and store.get('ai_cursor') == 20
    assert len(store.cards()) == count
    assert store.get('status') != 'retry_pending'
    await worker.tick()
    assert store.get('calls') == 2 and store.get('ai_cursor') == 40


@pytest.mark.parametrize('text', ['清风老师是ViewerB，不是ViewerA。', 'ViewerB以后叫清风老师。'])
def test_r2_unrelated_or_negated_relation_never_becomes_trusted_identity(make, text):
    store, worker = make([event(1), event(2, text, 'transcript', None, ''),
                          event(3, '合成曲甲送给清风老师。', 'transcript', None, '')])
    run = start(worker)
    worker.agent.exchange(run, call('record_alias', alias_args()))
    assert last(store)['status'] == 'unconfirmed'
    assert store.state()['agent']['aliases'][0]['status'] == 'unconfirmed'
    worker.agent.exchange(run, call('update_assistant', {'actions': [gift('111', '合成曲甲')]}))
    assert store.card('song:111') is None


def test_r3_revised_alias_relation_cannot_assign_new_gift(make):
    store, worker = make([event(1), event(2, 'ViewerA以后叫清风老师。', 'transcript', None, '')])
    run = start(worker)
    worker.agent.exchange(run, call('record_alias', alias_args()))
    worker.agent.exchange(run, call('finish_analysis', {**S, 'reason': '完成'}))
    store.ingest([event(3, 'ViewerB以后叫清风老师。', 'transcript', None, '', eid=2),
                  event(4, '合成曲甲送给清风老师。', 'transcript', None, '')])
    run = start(worker)
    assert store.state()['agent']['aliases'][0]['status'] == 'pending_review'
    assert worker.agent.durable(run)['aliases'][0]['status'] == 'pending_review'
    worker.agent.exchange(run, call('update_assistant', {'actions': [gift('111', '合成曲甲', voice='4')]}))
    assert store.card('song:111') is None


def test_reconfirm_revised_alias_preserves_original_relation_and_restores_use(make):
    store, worker = make([event(1), event(2, 'ViewerA以后叫清风老师。', 'transcript', None, '')])
    run = start(worker)
    worker.agent.exchange(run, call('record_alias', alias_args()))
    assert last(store)['status'] == 'confirmed'
    worker.agent.exchange(run, call('finish_analysis', {**S, 'reason': '完成'}))
    store.ingest([event(3, '修订后：ViewerA叫清风老师。', 'transcript', None, '', eid=2),
                  event(4, '合成曲甲送给清风老师。', 'transcript', None, '')])
    run = start(worker)
    args = {**alias_args(), 'expected_version': 1,
            'evidence': [{'id': '1', 'version': 1}, {'id': '2', 'version': 2}],
            'identity_evidence': [{'id': '1', 'version': 1}],
            'relation_evidence': [{'id': '2', 'version': 2}], 'quoted_relation': 'ViewerA叫清风老师。'}
    worker.agent.exchange(run, call('record_alias', args))
    assert last(store)['status'] == 'confirmed' and last(store)['version'] == 2
    old = json.loads(store.db.execute('SELECT data FROM alias_history').fetchone()[0])
    assert old['quote'] == 'ViewerA以后叫清风老师' and old['evidence'][1]['version'] == 1
    worker.agent.exchange(run, call('update_assistant', {'actions': [gift('111', '合成曲甲', voice='4')]}))
    assert store.card('song:111')['uid'] == '111'


@pytest.mark.parametrize('text,uid,author,confirmed', [
    ('我叫清风老师。', '111', 'ViewerA', True),
    ('我叫清风老师。', '222', 'ViewerB', False),
    ('ViewerA不是清风老师。', None, '', False),
    ('ViewerA是清风老师吗？', None, '', False),
    ('ViewerA可能是清风老师。', None, '', False),
    ('ViewerA叫清风老师，ViewerB也叫清风老师。', None, '', False),
])
def test_relationship_self_claim_and_ambiguity(make, text, uid, author, confirmed):
    store, worker = make([event(1), event(2, text, 'danmaku' if uid else 'transcript', uid, author),
                          event(3, uid='222', author='ViewerB')])
    run = start(worker)
    worker.agent.exchange(run, call('record_alias', alias_args()))
    assert (last(store)['status'] == 'confirmed') is confirmed


def test_quoted_relation_must_include_relationship_not_only_alias(make):
    store, worker = make([event(1), event(2, 'ViewerA叫清风老师。', 'transcript', None, '')])
    run = start(worker)
    worker.agent.exchange(run, call('record_alias', {**alias_args(), 'quoted_relation': '清风老师'}))
    assert last(store)['status'] == 'unconfirmed'


@pytest.mark.parametrize('quote_mode', ['full', 'prefix', 'omitted'])
@pytest.mark.parametrize('kind,text,prefix', [
    ('self', '我是清风老师的粉丝。', '我是清风老师'),
    ('self', '我叫清风老师的朋友来听歌。', '我叫清风老师'),
    ('self', '我是清风老师傅。', '我是清风老师'),
    ('self', '我是清风老师，的粉丝。', '我是清风老师'),
    ('named', 'ViewerA是清风老师的粉丝。', 'ViewerA是清风老师'),
    ('named', 'ViewerA叫清风老师的朋友来听歌。', 'ViewerA叫清风老师'),
    ('named', 'ViewerA叫清风老师傅。', 'ViewerA叫清风老师'),
    ('named', '小ViewerA是清风老师。', 'ViewerA是清风老师'),
    ('named', '清风老师是ViewerA的粉丝。', '清风老师是ViewerA'),
    ('named', '清风老师是ViewerAB。', '清风老师是ViewerA'),
    ('named', '小清风老师是ViewerA。', '清风老师是ViewerA'),
])
def test_f1_incomplete_equivalence_never_authorizes_gift(make, kind, text, prefix, quote_mode):
    # self分支与审阅脚本一致：只有本人身份/关系弹幕和后续赠歌语音两条事件。
    rows = [event(1, text if kind == 'self' else '你好')]
    relation_id = '1' if kind == 'self' else '2'
    if kind != 'self':
        rows.append(event(2, text, 'transcript', None, ''))
    voice_id = str(len(rows)+1)
    rows.append(event(int(voice_id), '合成曲甲送给清风老师。', 'transcript', None, ''))
    store, worker = make(rows)
    run = start(worker)
    identity = {'id': '1', 'version': 1}
    relation = {'id': relation_id, 'version': 1}
    args = {**alias_args(), 'evidence': [identity, relation], 'identity_evidence': [identity],
            'relation_evidence': [relation]}
    if quote_mode != 'omitted':
        args['quoted_relation'] = text if quote_mode == 'full' else prefix
    worker.agent.exchange(run, call('record_alias', args))
    assert last(store)['status'] == 'unconfirmed'
    assert store.state()['agent']['aliases'][0]['status'] == 'unconfirmed'
    assert store.db.execute('SELECT count(*) FROM aliases').fetchone()[0] == 0
    worker.agent.exchange(run, call('update_assistant', {'actions': [gift('111', '合成曲甲', voice=voice_id)]}))
    assert 'error' in last(store) and store.card('song:111') is None


@pytest.mark.parametrize('kind,text', [
    ('self', '我是清风老师。'),
    ('self', '你好，我叫清风老师。'),
    ('named', 'ViewerA是清风老师。'),
    ('named', 'ViewerA，以后叫你清风老师。'),
    ('named', '清风老师就是ViewerA。'),
    ('named', '清风老师也叫ViewerA。'),
])
def test_f1_complete_equivalence_still_authorizes_gift(make, kind, text):
    store, worker = make([event(1), event(2, text, 'danmaku' if kind == 'self' else 'transcript',
                                           '111' if kind == 'self' else None, 'ViewerA' if kind == 'self' else ''),
                          event(3, '合成曲甲送给清风老师。', 'transcript', None, '')])
    run = start(worker)
    worker.agent.exchange(run, call('record_alias', {**alias_args(), 'quoted_relation': text}))
    assert last(store)['status'] == 'confirmed'
    worker.agent.exchange(run, call('update_assistant', {'actions': [gift('111', '合成曲甲')]}))
    assert store.card('song:111')['uid'] == '111'


def test_alias_chain_invalidates_when_parent_proof_changes(make):
    store, worker = make([event(1), event(2, 'ViewerA叫清风老师。', 'transcript', None, ''),
                          event(3, '清风老师也叫阿风。', 'transcript', None, '')])
    run = start(worker)
    worker.agent.exchange(run, call('record_alias', alias_args()))
    worker.agent.exchange(run, call('record_alias', {**alias_args(), 'alias': '阿风',
        'evidence': [{'id': '1', 'version': 1}, {'id': '3', 'version': 1}]}))
    assert last(store)['status'] == 'confirmed' and last(store)['dependencies']
    store.ingest([event(4, '此称呼有误', 'transcript', None, '', eid=2)])
    assert all(a['status'] == 'pending_review' for a in store.state()['agent']['aliases'])


def test_alias_result_overflow_retains_cursor_and_can_shrink(make):
    store, worker = make([event(1)])
    with store.db:
        for i in range(20):
            data = {'uid': str(i+1), 'alias': f'称呼{i}', 'version': 1,
                    'evidence': [{'id': '1', 'version': 1}], 'reason': '合成容量数据'*25}
            store.db.execute('INSERT INTO aliases VALUES(?,?,?)', (data['alias'], data['uid'], encode(data)))
    run = start(worker)
    worker.config.tool_result_bytes = 1500
    worker.agent.exchange(run, call('read_state', {**S, 'limit': 1}))
    assert len(last(store)['cards']) == 1 and 'aliases' not in last(store)
    worker.agent.exchange(run, call('read_aliases', {**S, 'limit': 20, 'offset': 0}))
    page = last(store)
    assert page['total'] == 20 and page['next_offset'] == 0 and page['suggested_limit'] == 10
    worker.agent.exchange(run, call('read_aliases', {**S, 'limit': 1, 'offset': page['next_offset']}))
    assert len(last(store)['aliases']) == 1 and last(store)['next_offset'] == 1


@pytest.mark.asyncio
async def test_capacity_pause_is_distinct_from_network_retry(make):
    store, worker = make([event(1)])
    worker.config.max_bytes = 1
    await worker.tick()
    assert store.get('status') == 'context_capacity_paused'
    assert store.get('agent_status') == 'context_capacity_paused'
    assert store.get('agent_metrics')['capacity_reason'] == 'byte_limit'
    assert store.get('calls', 0) == 0 and store.get('ai_cursor', 0) == 0
    worker.config.max_bytes = 800000
    worker.runtime.infer.return_value = call('finish_analysis', {**S, 'reason': '恢复'})
    await worker.tick()
    assert store.get('ai_cursor') == 1


def test_usage_calibrates_estimate_without_changing_byte_limit(make):
    store, worker = make([event(1)])
    run = start(worker)
    worker.agent.messages(run, PROMPT)
    base = store.get('agent_metrics')['last_input_base_tokens']
    response = call('read_state', S)
    response['prompt_eval_count'] = base*2
    worker.agent.exchange(run, response)
    assert store.get('agent_metrics')['estimator_calibration'] == pytest.approx(2.2)
    worker.agent.messages(run, PROMPT)
    metrics = store.get('agent_metrics')
    assert metrics['estimated_input_tokens'] > metrics['last_input_base_tokens']*2
    assert metrics['byte_limit'] == 800000 and metrics['context_token_limit'] == 200000


def test_ascii_payload_over_200kb_is_not_200k_tokens(make):
    store, worker = make([event(1)])
    run = start(worker)
    with store.db:
        store.db.execute('INSERT INTO agent_exchanges(run_id,at,data) VALUES(?,?,?)',
                         ('old', 0, encode([{'role': 'assistant', 'content': 'x'*270000}])))
    worker.agent.messages(run, PROMPT)
    metrics = store.get('agent_metrics')
    assert 200000 < metrics['input_bytes'] < 800000
    assert metrics['estimated_input_tokens'] < 200000
    assert metrics.get('compaction_count', 0) == 0
    worker.config.max_bytes = 200000
    worker.agent.messages(run, PROMPT)
    assert store.get('agent_metrics')['compaction_count'] == 1


@pytest.mark.asyncio
async def test_r4_alias_pages_can_reach_all_records_and_finish(make):
    store, worker = make([event(1)])
    with store.db:
        for i in range(150):
            value = {'uid': str(i+1), 'alias': '称呼'+str(i), 'version': 1,
                     'evidence': [{'id': '1', 'version': 1}], 'reason': '容量测试，不作为身份事实'}
            store.db.execute('INSERT INTO aliases VALUES(?,?,?)', (value['alias'], value['uid'], encode(value)))
    seen = set()
    async def infer(context):
        previous = [m for m in context['agent_messages'] if m['role'] == 'tool']
        offset = 0
        if previous:
            page = json.loads(previous[-1]['content'])
            assert 'aliases' in page
            seen.update(a['alias'] for a in page['aliases'])
            offset = page['next_offset']
            if offset is None:
                return call('finish_analysis', {**S, 'reason': '分页结束'})
        return call('read_aliases', {**S, 'limit': 50, 'offset': offset})
    worker.runtime.infer.side_effect = infer
    await worker.tick()
    assert len(seen) == 150
    assert store.get('ai_cursor') == 1


def test_n1_two_recipient_gifts_in_one_transcript_are_independent(make):
    store, worker = make([event(1), event(2, uid='222', author='ViewerB'),
                          event(3, '合成曲甲送给ViewerA，合成曲乙送给ViewerB。', 'transcript', None, '')])
    run = start(worker)
    for uid, title, identity in [('111', '合成曲甲', '1'), ('222', '合成曲乙', '2')]:
        worker.agent.exchange(run, call('update_assistant', {'actions': [gift(uid, title, identity)]}))
    assert store.card('song:111') and store.card('song:222')


def test_n2_cached_finish_cannot_restore_stale_active_run(make):
    store, worker = make([event(1)])
    run = start(worker)
    stale = deepcopy(run)
    response = call('finish_analysis', {**S, 'reason': '完成'})
    worker.agent.exchange(run, response)
    worker.agent.exchange(stale, response)
    saved = json.loads(store.db.execute('SELECT data FROM agent_runs WHERE id=?', (run['id'],)).fetchone()[0])
    assert saved['status'] == 'finished'
    assert store.get('agent_status') == 'sleeping' and store.get('active_agent_run') is None
