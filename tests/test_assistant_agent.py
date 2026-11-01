import asyncio
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from bilibili_live_monitor.assistant import AssistantStore, encode
from bilibili_live_monitor.assistant_agent import TOOLS, RoomAgent
from bilibili_live_monitor.assistant_worker import PROMPT, AssistantWorker, OllamaRuntime, WorkerConfig
from bilibili_live_monitor.danmaku import chat_message, gift_message
from bilibili_live_monitor.store import Store

SCOPE = {'room_id': '123456', 'session_id': 'scene'}


def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


@pytest.fixture
def scene(tmp_path):
    now = time.time()
    source = Store(tmp_path/'source.sqlite')
    state = AssistantStore(tmp_path/'assistant.sqlite')
    state.configure('123456', 'scene', stamp(now-60), 'generation')
    chat = chat_message([[0, 0, 0, 0, now-30], '你好', [111, 'ViewerA'], [], 0, 0, 0, 0])
    gift = gift_message({'cmd': 'SEND_GIFT', 'data': {'giftId': 34001, 'giftName': '粉丝团灯牌',
                        'num': 1, 'uid': 111, 'uname': 'ViewerA', 'tid': 'gift-a', 'timestamp': now-29}})
    for message in (chat, gift):
        source.live_message('123456', {**message, 'session_id': 'generation', 'received_utc': stamp(now-28)})
    yield now, source, state
    source.close()
    state.close()


def speech(source, now, seq, text):
    source.segment({'room_id': '123456', 'seq': seq, 'text': text, 'is_final': True, 'status': 'done',
                    'session_id': 'generation', 'end_utc': stamp(now), 'received_utc': stamp(now)})


def live_client(now):
    platform = {'candidates': [{'room_id': '123456', 'live_status': 1, 'status_checked_utc': stamp(now)}],
                'sources': [{'room_id': '123456', 'status': 'capturing'}],
                'message_health': {'123456': {'state': 'connected'}}}
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=platform)))


def call(name, args, ident='tool-a'):
    return {'message': {'tool_calls': [{'id': ident, 'type': 'function',
                                       'function': {'name': name, 'arguments': args}}]},
            'prompt_eval_count': 42}


def result(messages, tool):
    return next(json.loads(m['content']) for m in reversed(messages)
                if m['role'] == 'tool' and m['tool_name'] == tool)


def prepared(scene, **config):
    _, source, state = scene
    worker = AssistantWorker(state, source.path, 'http://synthetic', WorkerConfig(interval=0, **config))
    state.set_enabled(True)
    worker.ingest()
    context, target, offered = worker.context()
    run = worker.agent.start(context, target, offered)
    return worker, run


@pytest.mark.asyncio
async def test_real_store_multiturn_alias_voice_gift_survives_restart_and_compaction(scene, monkeypatch):
    now, source, state = scene
    speech(source, now-20, 1, 'ViewerA，以后叫你清风老师。')
    monkeypatch.setenv('OLLAMA_API_KEY', 'synthetic-key')
    phase, step, count = 0, 0, 0
    identity_ref = alias_ref = gift_voice_ref = None

    def respond(request):
        nonlocal step, count, identity_ref, alias_ref, gift_voice_ref
        count += 1
        body = json.loads(request.content)
        assert body['model'] == 'deepseek-v4.1-flash' and body['think'] is False
        assert {t['function']['name'] for t in body['tools']} == {t['function']['name'] for t in TOOLS}
        messages = body['messages']
        durable = json.loads(messages[1]['content'].split('：', 1)[1])
        n = step
        step += 1
        if phase == 0:
            if n == 0:
                return httpx.Response(200, json=call('viewer_events', {**SCOPE, 'uid': '111'}))
            if n == 1:
                event = result(messages, 'viewer_events')['events'][-1]
                identity_ref = {'id': event['id'], 'version': event['version']}
                return httpx.Response(200, json=call('search_transcripts', {**SCOPE, 'keyword': '清风老师'}))
            if n == 2:
                event = result(messages, 'search_transcripts')['events'][0]
                alias_ref = {'id': event['id'], 'version': event['version']}
                return httpx.Response(200, json=call('record_alias', {**SCOPE, 'uid': '111', 'alias': '清风老师',
                    'expected_version': 0, 'evidence': [identity_ref, alias_ref], 'reason': '主播明确给ViewerA称呼'}))
            assert result(messages, 'record_alias')['alias'] == '清风老师'
            return httpx.Response(200, json=call('finish_analysis', {**SCOPE, 'reason': '称呼已查证，等待后续'}))
        assert durable['aliases'][0]['alias'] == '清风老师'
        if n == 0:
            return httpx.Response(200, json=call('read_state', SCOPE))
        if n == 1:
            return httpx.Response(200, json=call('read_evidence', {**SCOPE, 'evidence': [identity_ref]}))
        if n == 2:
            return httpx.Response(200, json=call('search_transcripts', {**SCOPE, 'keyword': '合成曲甲'}))
        if n == 3:
            found = result(messages, 'search_transcripts')['events']
            voice = found[0]
            voice_ref = {'id': voice['id'], 'version': voice['version']}
            if phase == 1:
                gift_voice_ref = voice_ref
                action = {'room_id': '123456', 'session_id': 'scene', 'card_id': 'song:voice-a',
                          'kind': 'song', 'title': '合成曲甲', 'status': 'queued', 'expected_version': 0,
                          'song_origin': 'gift', 'recipient_uid': '111', 'evidence': [identity_ref, voice_ref]}
            else:
                song = next(c for c in result(messages, 'read_state')['cards'] if c['kind'] == 'song')
                action = {'room_id': '123456', 'session_id': 'scene', 'card_id': song['id'],
                          'kind': 'song', 'title': '合成曲甲', 'status': 'sung', 'expected_version': song['version'],
                          'evidence': [identity_ref, gift_voice_ref, voice_ref]}
            return httpx.Response(200, json=call('update_assistant', {'actions': [action]}))
        assert 'updated' in result(messages, 'update_assistant')
        return httpx.Response(200, json=call('finish_analysis', {**SCOPE, 'reason': '本批完成'}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model, live_client(now) as upstream:
        cfg = WorkerConfig(interval=0)
        worker = AssistantWorker(state, source.path, 'http://synthetic', cfg, OllamaRuntime(cfg, model), upstream)
        state.set_enabled(True)
        await worker.tick()
        assert state.get('calls') == 4 and state.get('agent_status') == 'sleeping'
        assert state.db.execute('SELECT count(*) FROM aliases').fetchone()[0] == 1
        before = count
        await worker.tick()
        assert count == before  # 没有新事件不调用模型。
        # 模拟重启前旧conversation已超过时间窗且上次实际usage超过阈值。
        with state.db:
            state.db.execute('UPDATE agent_exchanges SET at=?', (time.time()-180,))
            worker.agent.metric(last_prompt_eval_count=200001)
        reopened = AssistantStore(state.path)
        try:
            phase, step = 1, 0
            speech(source, now-5, 2, '这首合成曲甲送给清风老师。')
            resumed = AssistantWorker(reopened, source.path, 'http://synthetic', cfg, OllamaRuntime(cfg, model), upstream)
            reopened.set_enabled(True)
            await resumed.tick()
            assert reopened.card('song:voice-a')['status'] == 'queued'
            assert reopened.card('song:voice-a')['uid'] == '111'
            assert reopened.get('agent_metrics')['compaction_count'] >= 1
            with reopened.db:
                reopened.db.execute('UPDATE agent_exchanges SET at=?', (time.time()-180,))
                resumed.agent.metric(last_prompt_eval_count=200001)
            phase, step = 2, 0
            speech(source, now, 3, '清风老师，合成曲甲唱完了。')
            await resumed.tick()
            assert reopened.card('song:voice-a')['status'] == 'sung'
            assert not reopened.card('medal:gift:111')['eligible']
            assert reopened.get('calls') == 14
            assert reopened.get('agent_metrics')['last_prompt_eval_count'] == 42
            assert reopened.db.execute('SELECT count(*) FROM agent_audit').fetchone()[0] == 14
            for row in reopened.db.execute('SELECT data FROM agent_exchanges'):
                unit = json.loads(row[0])
                if unit[0]['role'] == 'assistant':
                    assert len(unit[0]['tool_calls']) == len(unit)-1
                    assert all(m['role'] == 'tool' for m in unit[1:])
        finally:
            reopened.close()


def test_alias_scope_literal_evidence_conflict_and_idempotency(scene):
    now, source, state = scene
    speech(source, now, 1, 'ViewerA叫清风老师。')
    worker, run = prepared(scene)
    context, _, _ = worker.context()
    refs = [{'id': e['id'], 'version': e['version']} for e in context['evidence']]
    args = {**SCOPE, 'uid': '111', 'alias': '清风老师', 'expected_version': 0, 'evidence': refs, 'reason': '明确称呼'}
    worker.agent.exchange(run, call('record_alias', args))
    worker.agent.exchange(run, call('record_alias', args, 'different-call-id'))
    alias = json.loads(state.db.execute('SELECT data FROM aliases').fetchone()[0])
    assert alias['version'] == 1
    assert state.state()['agent']['trace'][0]['cached']
    reopened = AssistantStore(state.path)
    try:
        agent = RoomAgent(reopened, worker.config)
        agent.exchange(agent.active(), call('record_alias', args, 'restarted-call'))
        assert json.loads(reopened.db.execute('SELECT data FROM aliases').fetchone()[0])['version'] == 1
    finally:
        reopened.close()
    worker.agent.exchange(run, call('record_alias', {**args, 'alias': 'TranslatedName'}))
    assert state.state()['agent']['trace'][0]['error'] == 'ValueError'


def test_alias_cannot_merge_two_uids_or_invent_recipient(scene):
    now, source, state = scene
    speech(source, now-5, 1, 'ViewerA叫清风老师。ViewerB也提到清风老师。')
    other = chat_message([[0, 0, 0, 0, now-6], '你好', [222, 'ViewerB'], [], 0, 0, 0, 0])
    source.live_message('123456', {**other, 'session_id': 'generation', 'received_utc': stamp(now-6)})
    worker, run = prepared(scene)
    context, _, _ = worker.context()
    refs = [{'id': e['id'], 'version': e['version']} for e in context['evidence']]
    args = {**SCOPE, 'uid': '111', 'alias': '清风老师', 'expected_version': 0, 'evidence': refs, 'reason': '明确称呼'}
    worker.agent.exchange(run, call('record_alias', args))
    worker.agent.exchange(run, call('record_alias', {**args, 'uid': '222', 'expected_version': 1}))
    assert state.state()['agent']['trace'][0]['error'] == 'Conflict'
    assert state.db.execute('SELECT uid FROM aliases').fetchone()[0] == '111'
    speech(source, now, 2, '合成曲甲送给从未确认的老师。')
    worker.ingest()
    context, _, offered = worker.context()
    with pytest.raises(ValueError, match='主动赠歌'):
        state.apply({'actions': [{**SCOPE, 'card_id': 'song:bad', 'kind': 'song', 'status': 'queued',
            'title': '合成曲甲', 'recipient_uid': '111', 'song_origin': 'gift', 'expected_version': 0,
            'evidence': offered['evidence']}]}, 100, offered)
    worker.agent.exchange(run, call('viewer_events', {**SCOPE, 'room_id': '654321', 'uid': '111'}))
    assert state.state()['agent']['trace'][0]['error'] == 'ValueError'
    worker.agent.exchange(run, call('read_state', {**SCOPE, 'session_id': 'other'}))
    assert state.state()['agent']['trace'][0]['error'] == 'ValueError'


def test_write_and_paired_history_rollback_together(scene, monkeypatch):
    now, source, state = scene
    speech(source, now, 1, 'ViewerA叫清风老师。')
    worker, run = prepared(scene)
    context, _, _ = worker.context()
    refs = [{'id': e['id'], 'version': e['version']} for e in context['evidence']]
    response = call('record_alias', {**SCOPE, 'uid': '111', 'alias': '清风老师',
                    'expected_version': 0, 'evidence': refs, 'reason': '明确称呼'})
    monkeypatch.setattr(worker.agent, 'save', lambda run: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError):
        worker.agent.exchange(run, response)
    assert state.db.execute('SELECT count(*) FROM aliases').fetchone()[0] == 0
    assert state.db.execute('SELECT count(*) FROM agent_calls').fetchone()[0] == 0
    assert state.db.execute('SELECT count(*) FROM agent_audit').fetchone()[0] == 0
    assert all(json.loads(r[0])[0]['role'] == 'user' for r in state.db.execute('SELECT data FROM agent_exchanges'))


def test_new_event_input_is_after_previous_conversation_not_before_it(scene):
    now, source, state = scene
    worker, run = prepared(scene)
    worker.agent.exchange(run, call('finish_analysis', {**SCOPE, 'reason': '旧批次完成'}))
    speech(source, now, 1, '全新事件')
    worker.ingest()
    context, target, offered = worker.context()
    current = worker.agent.start(context, target, offered)
    messages = worker.agent.messages(current, PROMPT)
    assert messages[-1]['role'] == 'user'
    assert '全新事件' in messages[-1]['content']
    assert messages[-2]['role'] == 'tool' and messages[-2]['tool_name'] == 'finish_analysis'


@pytest.mark.asyncio
async def test_budget_each_round_timeout_restart_and_turn_limit(scene):
    now, source, state = scene
    cfg = WorkerConfig(interval=0, max_calls=2, timeout=.01)
    runtime = AsyncMock()
    runtime.infer.side_effect = [call('read_state', SCOPE), TimeoutError(),
                                 call('finish_analysis', {**SCOPE, 'reason': '完成'})]
    async with live_client(now) as upstream:
        worker = AssistantWorker(state, source.path, 'http://synthetic', cfg, runtime, upstream)
        state.set_enabled(True)
        await worker.tick()
        assert state.get('calls') == 2 and state.get('ai_cursor') is None
        assert state.get('last_error') == 'TimeoutError'
        await worker.tick()
        assert state.get('status') == 'budget_paused' and runtime.infer.await_count == 2
        cfg.max_calls = 3
        resumed = AssistantWorker(state, source.path, 'http://synthetic', cfg, runtime, upstream)
        state.set_enabled(True)
        await resumed.tick()
        assert state.get('calls') == 3 and state.get('ai_cursor') is not None
        assert state.get('agent_status') == 'sleeping'
        speech(source, now, 1, '新的观察')
        cfg.max_calls, cfg.max_tool_turns = 20, 1
        runtime.infer.side_effect = None
        runtime.infer.return_value = call('read_state', SCOPE)
        await resumed.tick()
        assert state.get('agent_status') == 'turn_limit'
        calls = state.get('calls')
        await resumed.tick()
        assert state.get('calls') == calls


@pytest.mark.asyncio
async def test_old_model_result_cannot_overwrite_manual_change(scene):
    now, source, state = scene
    async def infer(context):
        card = state.card('chat:1')
        state.manual('scene', card['id'], card['version'], 'dismissed')
        return call('update_assistant', {'actions': [{**SCOPE, 'card_id': card['id'], 'kind': 'chat',
            'expected_version': card['version'], 'status': 'responded', 'title': card['title'],
            'evidence': card['evidence']}]})
    async with live_client(now) as upstream:
        worker = AssistantWorker(state, source.path, 'http://synthetic',
            WorkerConfig(interval=0, max_tool_turns=1), AsyncMock(infer=infer), upstream)
        state.set_enabled(True)
        await worker.tick()
        assert state.card('chat:1')['status'] == 'dismissed'
        assert state.get('ai_cursor') is None
        assert state.state()['agent']['trace'][0]['error'] == 'Conflict'


@pytest.mark.parametrize('dense', [False, True])
def test_200k_compaction_window_pairs_and_durable_state(scene, dense):
    _, _, state = scene
    worker, run = prepared(scene, context_tokens=200000, max_bytes=900000)
    original = state.db.execute('SELECT count(*) FROM observations').fetchone()[0]
    # ASCII的字节数可以大于token数；三轮共750KB才触发200K估算token预算。
    with state.db:
        for i in range(3):
            unit = [{'role': 'assistant', 'tool_calls': [{'function': {'name': 'read_state', 'arguments': SCOPE}}]},
                    {'role': 'tool', 'tool_name': 'read_state', 'content': encode({'text': 'x'*250000})}]
            state.db.execute('INSERT INTO agent_exchanges(run_id,at,data) VALUES(?,?,?)',
                             ('old', time.time()-(10+i if dense else 180+i), encode(unit)))
    messages = worker.agent.messages(run, PROMPT)
    metrics = state.get('agent_metrics')
    assert metrics['context_token_limit'] == 200000 and metrics['estimated_input_tokens'] <= 200000
    assert metrics['byte_limit'] == 900000 and metrics['input_bytes'] <= 900000
    if dense:
        assert metrics['input_bytes'] > 200000
    assert metrics['compaction_count'] == 1
    assert bool(metrics.get('dense_window_drops')) is dense
    assert 'medal:gift:111' in messages[1]['content']
    assert state.db.execute('SELECT count(*) FROM observations').fetchone()[0] == original
    assert state.card('medal:gift:111')['status'] == 'pending'
    for row in state.db.execute('SELECT data FROM agent_exchanges'):
        unit = json.loads(row[0])
        assert unit[0]['role'] == 'user' or len(unit) == 2


def test_actual_usage_triggers_old_window_compaction_and_durable_overflow(scene):
    _, _, state = scene
    worker, run = prepared(scene, context_tokens=20000, max_bytes=30000)
    with state.db:
        state.db.execute('INSERT INTO agent_exchanges(run_id,at,data) VALUES(?,?,?)',
                         ('old', time.time()-180, encode([{'role': 'assistant', 'content': 'old'}])))
        worker.agent.metric(last_prompt_eval_count=20001)
    worker.agent.messages(run, PROMPT)
    assert state.get('agent_metrics')['dropped_exchanges'] == 1
    worker.config.max_bytes = 1
    with pytest.raises(ValueError, match='工作索引'):
        worker.agent.messages(run, PROMPT)
    assert state.get('agent_metrics')['durable_overflow_count'] == 1
    assert state.cards()


def test_read_limits_coverage_and_tool_receipts_survive_restart(scene):
    now, source, state = scene
    speech(source, now, 1, '关键词'+('长文本'*2000))
    worker, run = prepared(scene, tool_result_bytes=1000)
    worker.agent.exchange(run, call('search_transcripts', {**SCOPE, 'keyword': '关键词', 'limit': 1}))
    unit = json.loads(state.db.execute('SELECT data FROM agent_exchanges ORDER BY id DESC LIMIT 1').fetchone()[0])
    assert json.loads(unit[1]['content'])['truncated']
    assert state.get('agent_metrics')['truncated_tool_results'] == 1
    before = state.get('cursor')
    worker.agent.exchange(run, call('viewer_events', {**SCOPE, 'uid': '111', 'limit': 10000}))
    assert state.state()['agent']['trace'][0]['error'] == 'ValidationError'
    reopened = AssistantStore(state.path)
    try:
        assert reopened.get('cursor') == before
        assert RoomAgent(reopened, worker.config).active()['id'] == run['id']
        assert reopened.db.execute('SELECT count(*) FROM agent_exchanges').fetchone()[0] == 3
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_real_timeout_keeps_active_run_without_unpaired_tool_messages(scene):
    now, source, state = scene
    async def hang(context):
        await asyncio.sleep(10)
    cfg = WorkerConfig(interval=0, timeout=.01)
    runtime = AsyncMock(infer=AsyncMock(side_effect=hang))
    async with live_client(now) as upstream:
        worker = AssistantWorker(state, source.path, 'http://synthetic', cfg, runtime, upstream)
        state.set_enabled(True)
        await worker.tick()
        rid = state.get('active_agent_run')
        assert state.get('calls') == 1 and state.get('ai_cursor') is None
        assert state.get('last_error') == 'TimeoutError'
        assert state.db.execute('SELECT count(*) FROM agent_calls').fetchone()[0] == 0
        runtime.infer.side_effect = None
        runtime.infer.return_value = call('finish_analysis', {**SCOPE, 'reason': '无需更新'})
        await worker.tick()
        assert state.get('calls') == 2 and state.get('active_agent_run') is None
        run = json.loads(state.db.execute('SELECT data FROM agent_runs WHERE id=?', (rid,)).fetchone()[0])
        assert run['status'] == 'finished'


def test_historical_asr_evidence_readable_but_stale_write_rejected(scene):
    now, source, state = scene
    speech(source, now-5, 1, 'ViewerA叫清风老师。')
    worker, run = prepared(scene)
    voice = next(state.evidence(str(i['id'])) for i in source.items('123456') if i['kind'] == 'transcript')
    speech(source, now, 1, '修订后的文本')
    worker.ingest()
    old_ref = {'id': voice['id'], 'version': 1}
    worker.agent.exchange(run, call('read_evidence', {**SCOPE, 'evidence': [old_ref]}))
    unit = json.loads(state.db.execute('SELECT data FROM agent_exchanges ORDER BY id DESC LIMIT 1').fetchone()[0])
    assert json.loads(unit[1]['content'])['events'][0]['data']['text'] == 'ViewerA叫清风老师。'
    worker.agent.exchange(run, call('record_alias', {**SCOPE, 'uid': '111', 'alias': '清风老师',
        'expected_version': 0, 'evidence': [old_ref, {'id': '1', 'version': 1}], 'reason': '旧稿不得写入'}))
    assert state.state()['agent']['trace'][0]['error'] == 'Conflict'
