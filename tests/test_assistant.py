import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bilibili_live_monitor.assistant import AssistantStore, Conflict, encode, pronunciation
from bilibili_live_monitor.assistant_api import create_sidecar
from bilibili_live_monitor.assistant_worker import AssistantWorker, OllamaRuntime, WorkerConfig, live_status

SINCE = '2030-01-01T00:00:00+00:00'


def event(n=1, **kwargs):
    return {'id': n, 'kind': 'item', 'data': encode({'id': n, 'kind': 'danmaku',
            'room_id': '123456', 'session_id': 'generation', 'received_utc': SINCE,
            'at_utc': SINCE, 'text': '想听歌曲A', 'author': '用户A', 'sender_uid': '1', **kwargs})}


@pytest.fixture
def store(tmp_path):
    s = AssistantStore(tmp_path/'assistant.sqlite')
    s.configure('123456', 'session', SINCE, 'generation')
    yield s
    s.close()


def action(store, **kwargs):
    return {'room_id': '123456', 'session_id': 'session', 'card_id': 'song:1', 'kind': 'song',
            'expected_version': 0, 'status': 'pending', 'title': '歌曲A',
            'evidence': [{'id': '1', 'version': 1}], **kwargs}


def offered(store):
    return {'cards': [c['id'] for c in store.cards()], 'evidence': [{'id': '1', 'version': 1}]}


def test_ingestion_restart_idempotent_and_fixed_scope(store):
    store.ingest([event(), event(2, room_id='654321'), event(3, received_utc='2029-01-01T00:00:00Z')])
    store.ingest([event()])
    other = AssistantStore(store.path)
    try:
        other.configure('123456', 'session', SINCE, 'generation')
        assert other.get('cursor') == 3
        assert len(other.cards()) == 1
        assert other.db.execute('SELECT count(*) FROM observations').fetchone()[0] == 1
        with pytest.raises(Conflict):
            other.configure('654321', 'session', SINCE, 'generation')
        with pytest.raises(ValueError):
            other.configure('123456', 'session', '2030-01-01', 'generation')
    finally:
        other.close()
    store.ingest([event(4, session_id='another-session')])
    assert store.get('source_mismatch') is True
    assert len(store.cards()) == 1


def test_state_path_cannot_modify_source_database(tmp_path):
    path = tmp_path/'source.sqlite'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE timeline(id INTEGER)')
    db.commit()
    db.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match='采集数据库'):
        AssistantStore(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('patch', [
    {'session_id': 'other'}, {'room_id': '654321'}, {'status': 'done'},
    {'expected_version': 4}, {'evidence': [{'id': 'missing', 'version': 1}]},
    {'evidence': [{'id': '1', 'version': 2}]}, {'kind': 'medal'}, {'unknown': 'bad'},
    {'card_id': 'chat:unknown', 'kind': 'chat'},
])
def test_illegal_actions_are_atomic(store, patch):
    store.ingest([event()])
    with pytest.raises((ValueError, ValidationError)):
        store.apply({'actions': [action(store), action(store, **patch)]}, 1, offered(store))
    assert store.card('song:1') is None
    assert store.get('ai_cursor') is None


def test_manual_cas_undo_and_ai_cannot_overwrite(store):
    store.ingest([event()])
    s2 = AssistantStore(store.path)
    try:
        s2.manual('session', 'chat:1', 1, 'responded')
        with pytest.raises(Conflict):
            store.manual('session', 'chat:1', 1, 'pending')
        for version in (1, 2):
            with pytest.raises(Conflict):
                store.apply({'actions': [action(store, card_id='chat:1', kind='chat',
                                               expected_version=version)]}, 1, offered(store))
        result = store.manual('session', 'chat:1', 2, undo=True)
        assert result['status'] == 'pending' and result['version'] == 3 and result['manual']
    finally:
        s2.close()


def test_asr_revisions_immutable_and_reject_old_ai_evidence(store):
    store.ingest([event()])
    item = json.loads(event(2, kind='transcript', text='歌曲')['data'])
    store.ingest([{'id': 2, 'kind': 'item', 'data': encode(item)}])
    item['text'] = '歌曲A唱完了'
    store.ingest([{'id': 3, 'kind': 'item', 'data': encode(item)}])
    assert store.evidence('2')['version'] == 2
    assert store.db.execute('SELECT count(*) FROM observations').fetchone()[0] == 3
    with pytest.raises(Conflict):
        store.apply({'actions': [action(store, evidence=[{'id': '2', 'version': 1}])]}, 3,
                    {'cards': [], 'evidence': [{'id': '2', 'version': 1}]})


def test_evidence_details_keep_referenced_revision(store):
    store.ingest([event()])
    update = json.loads(event()['data'])
    update['text'] = '修订文字'
    store.ingest([{'id': 2, 'kind': 'item', 'data': encode(update)}])
    detail = store.state()['evidence'][0]
    assert detail['version'] == 1 and detail['latest_version'] == 2
    assert detail['data']['text'] == '想听歌曲A'


def test_badges_and_rosters_alone_never_create_gift_followup(store):
    store.ingest([event(medal={'room_id': '123456', 'level': 1}, guard_level=None)])
    store.derive_medals()
    assert store.card('medal:1') is None
    store.derive_medals({'complete': False, 'members': []})
    assert store.card('medal:1') is None
    stamp = '2030-01-01T00:00:01+00:00'
    store.derive_medals({'complete': True, 'members': [], 'observed_utc': stamp})
    assert not any(c['kind'] == 'medal' for c in store.cards())
    store.derive_medals({'complete': True, 'members': [{'sender_uid': '1'}], 'observed_utc': stamp})
    store.ingest([event(2, sender_uid='2', medal={'room_id': '654321', 'level': 1}, guard_level=0)])
    store.derive_medals()
    assert not any(c['kind'] == 'medal' for c in store.cards())


def test_pronunciation_conservative_and_originals_unchanged(store):
    assert pronunciation('Alice')['text'] == ''
    assert 'chóng' in pronunciation('重')['text']
    assert 'zhòng' in pronunciation('重')['text']
    assert pronunciation('重')['uncertain']
    assert pronunciation('龘')['text']  # 词典支持则拼音，不支持仍保留原字
    assert pronunciation('重', 'chóng')['text'] == 'chóng'
    assert pronunciation('𠮷')['text'] == '𠮷'
    assert pronunciation('𠮷')['unknown'] == ['𠮷']
    assert pronunciation('𠮷')['uncertain']
    store.ingest([event()])
    original = store.evidence('1')
    store.state()
    assert store.evidence('1') == original
    assert 'pronunciation' not in original['data']


def platform(status=1):
    return {'candidates': [{'room_id': '123456', 'live_status': status, 'offline_checks': 2,
            'status_checked_utc': datetime.now(timezone.utc).isoformat()}],
            'sources': [{'room_id': '123456', 'status': 'capturing'}],
            'message_health': {'123456': {'state': 'connected'}}}


def source(tmp_path, rows):
    path = tmp_path/'source.sqlite'
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE events(id INTEGER PRIMARY KEY,kind TEXT,data TEXT);'
                     'CREATE TABLE guard_snapshots(room_id TEXT,data TEXT,id INTEGER PRIMARY KEY);')
    db.executemany('INSERT INTO events VALUES(:id,:kind,:data)', rows)
    db.commit()
    db.close()
    return path


@pytest.mark.asyncio
async def test_worker_retry_restart_budget_and_ended(store, tmp_path):
    path = source(tmp_path, [event()])
    runtime = AsyncMock()
    runtime.infer.side_effect = [TimeoutError(), {'actions': []}]
    state = platform()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=state))) as client:
        worker = AssistantWorker(store, path, 'http://upstream', WorkerConfig(interval=0), runtime, client)
        await worker.tick()
        assert store.get('ai_cursor') is None and store.get('cursor') == 1
        assert store.get('last_error') == 'TimeoutError'
        restarted = AssistantWorker(store, path, 'http://upstream', WorkerConfig(interval=0), runtime, client)
        await restarted.tick()
        assert store.get('ai_cursor') == 1 and store.get('calls') == 2
        state.update(platform(0))
        await restarted.tick()
        assert store.get('ended')
        state.update(platform())
        await restarted.tick()
        assert store.get('status') == 'ended'
        assert runtime.infer.await_count == 2


@pytest.mark.asyncio
async def test_worker_rechecks_revisions_during_inference(store, tmp_path):
    path = source(tmp_path, [event()])
    async def infer(context):
        db = sqlite3.connect(path)
        updated = json.loads(event()['data'])
        updated['text'] = '修改后的内容'
        db.execute('INSERT INTO events VALUES(2,?,?)', ('item', encode(updated)))
        db.commit()
        db.close()
        return {'actions': [action(store)]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=platform()))) as client:
        worker = AssistantWorker(store, path, 'http://upstream', WorkerConfig(interval=0),
                                 AsyncMock(infer=infer), client)
        await worker.tick()
        assert store.evidence('1')['version'] == 2
        assert store.card('song:1') is None and store.get('ai_cursor') is None


def test_asr_debounce_latest_context_and_payload_cap(store, tmp_path):
    store.ingest([event(kind='transcript', is_final=False)])
    worker = AssistantWorker(store, tmp_path/'unused', 'http://upstream')
    assert worker.context() is None
    store.db.execute('UPDATE evidence SET changed=?', (time.time()-5,))
    store.db.commit()
    context, target, _ = worker.context()
    assert len(context['evidence']) == 1 and target == 1
    worker.config.max_bytes = 1
    with pytest.raises(ValueError, match='max_bytes'):
        worker.context()


@pytest.mark.asyncio
async def test_budget_and_offline_skip_model(store, tmp_path):
    path = source(tmp_path, [event()])
    runtime = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
        worker = AssistantWorker(store, path, 'http://upstream', runtime=runtime, client=client)
        await worker.tick()
        assert store.get('status') == 'paused'
        runtime.infer.assert_not_called()
    with store.db:
        store.put('calls', 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=platform()))) as client:
        worker = AssistantWorker(store, path, 'http://upstream', WorkerConfig(max_calls=1), runtime, client)
        await worker.tick()
        assert store.get('status') == 'budget_paused'
        runtime.infer.assert_not_called()


@pytest.mark.asyncio
async def test_ollama_contract_only_mock(monkeypatch):
    monkeypatch.setenv('OLLAMA_API_KEY', 'test-only-key')
    def respond(request):
        assert request.headers['authorization'] == 'Bearer test-only-key'
        body = json.loads(request.content)
        assert body['model'] == 'deepseek-v4.1-flash'
        assert body['think'] is False and body['stream'] is False
        assert body['tools'][0]['function']['name'] == 'update_assistant'
        assert 'test-only-key' not in request.content.decode()
        return httpx.Response(200, json={'message': {'tool_calls': [
            {'function': {'name': 'update_assistant', 'arguments': {'actions': []}}}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await OllamaRuntime(WorkerConfig(), client).infer({'test': True}) == {'actions': []}


@pytest.mark.parametrize('message', [{}, {'tool_calls': []}, {'tool_calls': [{'function': {'name': 'send'}}]}])
@pytest.mark.asyncio
async def test_runtime_rejects_non_tool_responses(message, monkeypatch):
    monkeypatch.setenv('OLLAMA_API_KEY', 'test-only-key')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'message': message}))) as client:
        with pytest.raises(ValueError):
            await OllamaRuntime(WorkerConfig(), client).infer({})


def test_live_gate_freshness():
    assert live_status(platform(), '123456') == 'live'
    assert live_status(platform(0), '123456') == 'ended'
    p = platform()
    p['candidates'][0]['status_checked_utc'] = '2000-01-01T00:00:00Z'
    assert live_status(p, '123456') == 'paused'


@pytest.mark.asyncio
async def test_timeout_and_owner_identity(store, tmp_path, monkeypatch):
    path = source(tmp_path, [event()])
    async def hang(context):
        await asyncio.sleep(10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=platform()))) as client:
        worker = AssistantWorker(store, path, 'http://upstream', WorkerConfig(timeout=.01),
                                 AsyncMock(infer=hang), client)
        await worker.tick()
        assert store.get('last_error') == 'TimeoutError'
        assert store.get('ai_cursor') is None
        import psutil
        monkeypatch.setattr(psutil, 'Process', lambda pid: type('Process', (), {'create_time': lambda self: 42.0})())
        worker.config.owner_pid = 123456
        worker.config.owner_create_time = 41.0
        assert await worker.gate() == 'ended'


def test_assistant_api_scope_cas_override(store, tmp_path):
    store.ingest([event()])
    app = create_sidecar(store.path, static_dir=tmp_path/'missing')
    with TestClient(app) as client:
        assert client.get('/api/assistant/sessions').json()[0]['room_id'] == '123456'
        assert client.get('/api/assistant/state?session=wrong').status_code == 422
        body = {'session': 'session', 'expected_version': 1, 'status': 'responded'}
        assert client.patch('/api/assistant/cards/chat:1', json=body).status_code == 200
        assert client.patch('/api/assistant/cards/chat:1', json=body).status_code == 409
        assert client.post('/api/assistant/cards/chat:1/undo', json={**body, 'expected_version': 2}).status_code == 200
        assert client.put('/api/assistant/pronunciation', json={
            'session': 'session', 'name': '用户A', 'reading': 'yòng hù A'}).status_code == 200
        assert client.get('/api/assistant/state?session=session').json()['cards'][0]['pronunciation']['override']
        assert client.get('/api/assistant/missing').status_code == 404


def test_multiple_configured_rooms_are_isolated(store, tmp_path):
    other = AssistantStore(tmp_path/'other.sqlite')
    other.configure('654321', 'session-other', SINCE, 'generation-other')
    other.close()
    store.ingest([event()])
    app = create_sidecar([store.path, tmp_path/'other.sqlite'], static_dir=tmp_path/'missing')
    with TestClient(app) as client:
        assert len(client.get('/api/assistant/sessions').json()) == 2
        assert client.get('/api/assistant/state?session=session-other').json()['cards'] == []
        assert client.patch('/api/assistant/cards/chat:1', json={
            'session': 'session-other', 'expected_version': 1, 'status': 'responded'}).status_code == 409
        assert store.card('chat:1')['status'] == 'pending'
