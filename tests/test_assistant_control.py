"""AI授权与在途撤销；所有源事件、模型、HTTP均为离线合成数据。"""
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from bilibili_live_monitor.assistant import AssistantDisabled, AssistantStore, Conflict, encode
from bilibili_live_monitor.assistant_api import create_sidecar
from bilibili_live_monitor.assistant_cli import main
from bilibili_live_monitor.assistant_worker import AssistantWorker, WorkerConfig

SCOPE = {'room_id': '123456', 'session_id': 'synthetic'}
STAMP = '2020-01-01T00:00:00+00:00'


def response(*tools):
    return {'message': {'tool_calls': [{'function': {'name': name, 'arguments': args}} for name, args in tools]}}


def finish():
    return ('finish_analysis', {**SCOPE, 'reason': '合成批次完成'})


def update():
    return ('update_assistant', {'actions': [{**SCOPE, 'card_id': 'chat:1', 'kind': 'chat',
            'title': '你好', 'status': 'responded', 'expected_version': 1,
            'evidence': [{'id': '1', 'version': 1}]}]})


@pytest.fixture
def scene(tmp_path):
    source = tmp_path/'source.sqlite'
    with sqlite3.connect(source) as db:
        db.executescript('CREATE TABLE events(id INTEGER PRIMARY KEY,kind TEXT,data TEXT);'
                         'CREATE TABLE guard_snapshots(id INTEGER PRIMARY KEY,room_id TEXT,data TEXT);')
        for i, text, kind in [(1, '你好', 'danmaku'), (2, 'ViewerA叫清风老师。', 'transcript')]:
            data = {'id': str(i), 'room_id': SCOPE['room_id'], 'session_id': 'generation',
                    'kind': kind, 'text': text, 'is_final': True, 'at_utc': STAMP, 'received_utc': STAMP,
                    'sender_uid': '111' if i == 1 else None, 'author': 'ViewerA' if i == 1 else ''}
            db.execute('INSERT INTO events VALUES(?,?,?)', (i, 'item', encode(data)))
    store = AssistantStore(tmp_path/'assistant.sqlite')
    store.configure(SCOPE['room_id'], SCOPE['session_id'], STAMP, 'generation')
    runtime = AsyncMock()
    runtime.infer.return_value = response(finish())
    worker = AssistantWorker(store, source, 'http://synthetic', WorkerConfig(interval=0), runtime)
    worker.gate = AsyncMock(return_value='live')
    yield store, worker, runtime
    store.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_fresh_and_legacy_database_without_authorization_are_disabled(tmp_path, legacy):
    path = tmp_path/'assistant.sqlite'
    store = AssistantStore(path)
    if legacy:
        with store.db:
            store.put('enabled', True)  # 旧/无关字段不能当成新授权。
            store.put('agent_status', 'working')
            store.put('calls', 7)
    store.close()
    reopened = AssistantStore(path)
    try:
        assert reopened.control()['enabled'] is False
        assert reopened.state()['agent']['status'] == 'disabled'
        assert reopened.get('calls', 0) == (7 if legacy else 0)
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_disabled_still_ingests_and_manual_works_explicit_enable_calls_model(scene):
    store, worker, runtime = scene
    await worker.tick()
    runtime.infer.assert_not_called()
    assert store.get('cursor') == 2 and store.get('ai_cursor') is None
    assert store.card('chat:1')['status'] == 'pending'
    store.manual(SCOPE['session_id'], 'chat:1', 1, 'dismissed')
    assert store.card('chat:1')['manual'] is True
    store.set_enabled(True)
    await worker.tick()
    runtime.infer.assert_awaited_once()
    assert store.get('ai_cursor') == 2
    assert store.card('chat:1')['status'] == 'dismissed'


@pytest.mark.asyncio
@pytest.mark.parametrize('reenable', [False, True])
async def test_disable_in_flight_rejects_all_tools_and_cursor_even_if_reenabled(scene, reenable):
    store, worker, runtime = scene
    alias = ('record_alias', {**SCOPE, 'uid': '111', 'alias': '清风老师', 'expected_version': 0,
             'evidence': [{'id': '1', 'version': 1}, {'id': '2', 'version': 1}], 'reason': '原文称呼'})
    async def infer(_):
        other = AssistantStore(store.path)
        try:
            other.set_enabled(False)
            if reenable:
                other.set_enabled(True)
        finally:
            other.close()
        return response(update(), alias, finish())
    runtime.infer.side_effect = infer
    store.set_enabled(True)
    await worker.tick()
    assert store.card('chat:1')['version'] == 1 and store.get('ai_cursor') is None
    assert worker.agent.active()['turns'] == 0
    for table in ('aliases', 'agent_calls', 'agent_audit'):
        assert store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM agent_exchanges').fetchone()[0] == 1
    runtime.infer.side_effect = None
    runtime.infer.return_value = response(update(), alias, finish())
    store.set_enabled(True)
    await worker.tick()
    assert store.get('ai_cursor') == 2 and store.card('chat:1')['version'] == 2
    assert store.state()['agent']['aliases'][0]['status'] == 'confirmed'


@pytest.mark.asyncio
async def test_disable_before_request_prevents_model_call(scene):
    store, worker, runtime = scene
    calls = 0
    async def gate():
        nonlocal calls
        calls += 1
        if calls == 2:
            store.set_enabled(False)
        return 'live'
    worker.gate.side_effect = gate
    store.set_enabled(True)
    await worker.tick()
    runtime.infer.assert_not_called()
    assert store.get('calls', 0) == 0


@pytest.mark.asyncio
async def test_disable_between_rounds_stops_new_inference(scene):
    store, worker, runtime = scene
    runtime.infer.return_value = response(('read_state', SCOPE))
    original = worker.ingest
    def ingest():
        original()
        if store.db.execute('SELECT count(*) FROM agent_audit').fetchone()[0]:
            store.set_enabled(False)
    worker.ingest = ingest
    store.set_enabled(True)
    await worker.tick()
    runtime.infer.assert_awaited_once()
    assert worker.agent.active()['turns'] == 1 and store.get('ai_cursor') is None


@pytest.mark.asyncio
async def test_restart_disables_but_resumes_pending_run_without_duplicate_write(scene):
    store, worker, runtime = scene
    runtime.infer.side_effect = [response(update()), TimeoutError()]
    store.set_enabled(True)
    await worker.tick()
    run_id = worker.agent.active()['id']
    assert store.card('chat:1')['version'] == 2 and store.get('ai_cursor') is None
    restarted = AssistantWorker(store, worker.source_path, 'http://synthetic', WorkerConfig(interval=0), runtime)
    restarted.gate = AsyncMock(return_value='live')
    await restarted.tick()
    assert runtime.infer.await_count == 2 and store.control()['enabled'] is False
    assert restarted.agent.active()['id'] == run_id
    runtime.infer.side_effect = [response(update()), response(finish())]
    store.set_enabled(True)
    await restarted.tick()
    assert store.card('chat:1')['version'] == 2 and store.get('ai_cursor') == 2
    assert store.db.execute("SELECT count(*) FROM history WHERE card_id='chat:1'").fetchone()[0] == 2
    assert any(json.loads(r[0]).get('cached') for r in store.db.execute('SELECT data FROM agent_audit'))


def test_disabled_direct_exchange_cannot_finish_or_write(scene):
    store, worker, _ = scene
    worker.ingest()
    context, target, offered = worker.context()
    run = worker.agent.start(context, target, offered)
    with pytest.raises(AssistantDisabled):
        worker.agent.exchange(run, response(update(), finish()))
    with pytest.raises(AssistantDisabled):
        store.apply(update()[1], target, offered)
    with pytest.raises(AssistantDisabled):
        store.apply({'actions': []}, target, offered)
    assert store.get('ai_cursor') is None and store.card('chat:1')['version'] == 1


def test_control_api_strict_scope_defaults_manual_and_sidecar_restart(scene, tmp_path):
    store, worker, _ = scene
    worker.ingest()
    store.set_enabled(True)
    app = create_sidecar(store.path, static_dir=tmp_path/'absent')
    with TestClient(app) as client:
        assert client.get('/api/assistant/control', params={'session': 'synthetic'}).json()['enabled'] is False
        assert client.get('/api/assistant/state?session=synthetic').json()['control']['enabled'] is False
        assert client.put('/api/assistant/control', json={'session': 'other', 'enabled': True}).status_code == 422
        assert client.put('/api/assistant/control', json={'session': 'synthetic', 'enabled': 'true'}).status_code == 422
        enabled = client.put('/api/assistant/control', json={'session': 'synthetic', 'enabled': True}).json()
        assert enabled['enabled'] is True
        assert client.put('/api/assistant/control', json={'session': 'synthetic', 'enabled': True}).json() == enabled
        assert client.put('/api/assistant/control', json={'session': 'synthetic', 'enabled': False}).json()['enabled'] is False
        assert client.patch('/api/assistant/cards/chat:1', json={
            'session': 'synthetic', 'expected_version': 1, 'status': 'dismissed'}).status_code == 200
        client.put('/api/assistant/control', json={'session': 'synthetic', 'enabled': True})
    with TestClient(app) as client:
        assert client.get('/api/assistant/control?session=synthetic').json()['enabled'] is False
    assert store.card('chat:1')['manual'] is True
    assert store.db.execute('SELECT count(*) FROM ai_control_audit').fetchone()[0] >= 5


@pytest.mark.asyncio
@pytest.mark.parametrize('inflight', [False, True])
async def test_ended_session_does_not_auto_resume_and_cannot_enable(scene, inflight):
    store, worker, runtime = scene
    store.set_enabled(True)
    if inflight:
        async def infer(_):
            worker.gate.return_value = 'ended'
            return response(update(), finish())
        runtime.infer.side_effect = infer
    else:
        worker.gate.return_value = 'ended'
    await worker.tick()
    assert runtime.infer.await_count == int(inflight)
    with pytest.raises(Conflict):
        store.set_enabled(True)
    assert store.get('ended') is True
    assert store.control()['enabled'] is False
    assert store.get('ai_cursor') is None
    if inflight:
        assert store.card('chat:1')['version'] == 1


def test_control_cli_requires_explicit_switch_and_correct_session(scene, monkeypatch):
    store, _, _ = scene
    base = ['assistant', 'control', '--db', str(store.path), '--session', 'synthetic']
    monkeypatch.setattr('sys.argv', base)
    with pytest.raises(SystemExit):
        main()
    monkeypatch.setattr('sys.argv', base+['--enable'])
    main()
    assert store.control()['enabled'] is True
    monkeypatch.setattr('sys.argv', base+['--disable'])
    main()
    assert store.control()['enabled'] is False
