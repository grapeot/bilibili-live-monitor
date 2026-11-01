"""PR19 回归：真实采集 Store + 协议解析；源库、HTTP 和语音全为合成。"""
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from bilibili_live_monitor.assistant import Actions, AssistantStore, Conflict
from bilibili_live_monitor.assistant_worker import AssistantWorker, WorkerConfig
from bilibili_live_monitor.danmaku import chat_message, gift_message
from bilibili_live_monitor.store import Store


def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


@pytest.fixture
def scene(tmp_path):
    now = time.time()
    source = Store(tmp_path/'source.sqlite')
    state = AssistantStore(tmp_path/'assistant.sqlite')
    state.configure('123456', 'session', stamp(now-60), 'current')
    yield now, source, state
    source.close()
    state.close()


def chat(source, at, generation='current', guard=0, text='用户A的问题'):
    message = chat_message([[0, 0, 0, 0, at], text, [111, '用户A'],
                            [5, '灯牌', 0, 123456], 0, 0, 0, guard])
    source.live_message('123456', {**message, 'session_id': generation, 'received_utc': stamp(at)})


def purchase(source, at):
    # 协议事件时钟故意早于榜单，资格优先级应使用本地实际观测时间。
    message = gift_message({'cmd': 'GUARD_BUY', 'data': {
        'uid': 111, 'username': '用户A', 'guard_level': 3, 'num': 1, 'start_time': at-200}})
    source.live_message('123456', {**message, 'session_id': 'current', 'received_utc': stamp(at)})


def roster(source, at, *, members=(), complete=True):
    source.guard_snapshot({'room_id': '123456', 'observed_utc': stamp(at),
                           'complete': complete, 'reported_total': len(members),
                           'members': [{'sender_uid': str(uid)} for uid in members],
                           'source': 'guardTab/topList'})


def client(now):
    snapshot = {'candidates': [{'room_id': '123456', 'live_status': 1, 'status_checked_utc': stamp(now)}],
                'sources': [{'room_id': '123456', 'status': 'capturing'}],
                'message_health': {'123456': {'state': 'connected'}}}
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=snapshot)))


@pytest.mark.asyncio
async def test_old_current_late_old_missing_session_and_restart(scene):
    now, source, state = scene
    chat(source, now-90, 'old', text='场次起点前的旧来源')
    chat(source, now-50, 'old', text='本场当前采集前积压')
    chat(source, now-10, text='当前来源')
    chat(source, now-1, 'old', text='旧来源迟到，但仍然是旧来源')
    chat(source, now-20, 'late-old', text='较晚入库但观测时间早于当前来源')
    source.item({'room_id': '123456', 'kind': 'danmaku', 'at_utc': stamp(now),
                 'text': '', 'status': 'error', 'error': '页面导航期间直播事件缓冲已满'}, 'startup-gap:current')
    async with client(now) as transport:
        worker = AssistantWorker(state, source.path, 'http://synthetic', client=transport)
        worker.ingest()
        assert await worker.gate() == 'live'
        assert not state.get('source_mismatch')
        assert [c['title'] for c in state.cards() if c['kind'] == 'chat'] == ['当前来源']
        reopened = AssistantStore(state.path)
        try:
            restarted = AssistantWorker(reopened, source.path, 'http://synthetic', client=transport)
            restarted.ingest()
            assert await restarted.gate() == 'live'
            assert reopened.get('source_generations')['old']['at'] < now-60
            chat(source, now, 'successor', text='真正后继来源')
            restarted.ingest()
            assert await restarted.gate() == 'paused'
            chat(source, now+1, text='当前来源迟到不能解除后继保护')
            restarted.ingest()
            assert await restarted.gate() == 'paused'
        finally:
            reopened.close()
    final = AssistantStore(state.path)
    assert final.get('source_mismatch')
    final.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('successor', [False, True])
async def test_pre_fix_mismatch_state_reclassified_from_real_source_history(scene, successor):
    now, source, state = scene
    chat(source, now-40, 'old')
    chat(source, now-20)
    if successor:
        chat(source, now-10, 'new')
    async with client(now) as transport:
        worker = AssistantWorker(state, source.path, 'http://synthetic', client=transport)
        worker.ingest()
        with state.db:
            state.db.execute("DELETE FROM meta WHERE key='source_generations'")
            state.put('source_mismatch', True)  # 模拟2ef185d保留的布尔熔断，cursor已追平。
        worker.ingest()
        assert await worker.gate() == ('paused' if successor else 'live')
        assert state.get('source_mismatch') is successor


def test_old_roster_never_overrides_new_guard_purchase(scene):
    now, source, state = scene
    roster(source, now-20)
    chat(source, now-10)
    worker = AssistantWorker(state, source.path, 'http://synthetic')
    worker.ingest()
    assert state.card('medal:111')['eligible']
    purchase(source, now-5)
    worker.ingest()
    card = state.card('medal:111')
    assert not card['eligible']
    assert 'guard_observation' not in card
    assert any(state.evidence(r['id'])['data'].get('guard_source') == 'purchase' for r in card['evidence'])


def test_roster_selection_and_guard_priority_use_observed_time_not_ingest_order(scene):
    now, source, state = scene
    chat(source, now-30)
    purchase(source, now-20)
    # 后收到的旧弹幕不能把刚观察到的 guard=3 覆盖为0。
    chat(source, now-25, text='乱序旧弹幕')
    roster(source, now-10, members=[111])
    roster(source, now-40)  # id 更大，但观测更旧的完整空榜。
    worker = AssistantWorker(state, source.path, 'http://synthetic')
    worker.ingest()
    # 用更新榜单移除资格后，旧榜单不能重现它。
    assert state.card('medal:111') is None
    roster(source, now-5)  # 更新的完整榜单可提供新的明确缺席证据。
    worker.ingest()
    assert state.card('medal:111')['eligible']
    assert state.card('medal:111')['guard_observation']['observed_utc'] == stamp(now-5)


@pytest.mark.parametrize('guard', [None, 0])
def test_partial_roster_absence_does_not_prove_non_guard(scene, guard):
    now, source, state = scene
    chat(source, now-10, guard=guard)
    roster(source, now-5, complete=False)
    AssistantWorker(state, source.path, 'http://synthetic').ingest()
    assert bool(state.card('medal:111')) is (guard == 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['done', 'dismissed', 'pending'])
async def test_ai_medal_status_uses_voice_evidence_and_preserves_qualification(scene, status):
    now, source, state = scene
    chat(source, now-10)
    source.segment({'room_id': '123456', 'seq': 1, 'session_id': 'current', 'text': '用户A，已经回复你了',
                    'end_utc': stamp(now-5), 'received_utc': stamp(now-5), 'is_final': True, 'status': 'done'})
    runtime = AsyncMock()
    async with client(now) as transport:
        worker = AssistantWorker(state, source.path, 'http://synthetic', WorkerConfig(interval=0), runtime, transport)
        worker.ingest()
        card = state.card('medal:111')
        qualification = {k: card.get(k) for k in ('eligible', 'uid', 'author', 'title', 'guard_observation')}
        context, target, offered = worker.context()
        assert 'medal:111' in offered['cards']
        refs = offered['evidence']
        assert any(e['data']['kind'] == 'transcript' for e in context['evidence'])
        payload = {'actions': [{'room_id': '123456', 'session_id': 'session', 'card_id': 'medal:111',
                               'kind': 'medal', 'expected_version': card['version'], 'status': status,
                               'title': '不能用AI改写真实资格', 'evidence': refs}]}
        assert 'medal' in Actions.model_json_schema()['$defs']['Action']['properties']['kind']['enum']
        with pytest.raises(Conflict):
            state.apply({'actions': [{**payload['actions'][0], 'evidence': [
                {**r, 'version': r['version']+1} for r in refs]}]}, target, offered)
        assert state.card('medal:111') == card
        runtime.infer.return_value = payload
        await worker.tick()
        updated = state.card('medal:111')
        assert updated['status'] == status and state.get('ai_cursor') == target
        assert {k: updated.get(k) for k in qualification} == qualification
        assert updated['evidence'] == refs  # derive 后仍保留语音证据。
        assert updated['version'] == card['version']+1
        with pytest.raises(Conflict):
            state.apply(payload, target, offered)
        state.manual('session', updated['id'], updated['version'], 'pending')
        payload['actions'][0]['expected_version'] = updated['version']+1
        with pytest.raises(Conflict):
            state.apply(payload, target, offered)


def test_pending_observation_count_not_source_event_cursor(scene):
    now, source, state = scene
    for i in range(10):
        source.event('room', {'room_id': '123456', 'status': 'capturing'})
    chat(source, now-10)
    worker = AssistantWorker(state, source.path, 'http://synthetic')
    worker.ingest()
    report = state.state()['worker']
    assert report['cursor'] == 11
    assert report['pending_observations'] == 1
    assert report['last_observation'] is not None
    _, target, offered = worker.context()
    state.apply({'actions': []}, target, offered)
    report = state.state()['worker']
    assert report['pending_observations'] == 0
    assert report['last_analysis'] is not None
