"""PR19 回归：真实采集 Store + 协议解析；源库、HTTP 和语音全为合成。"""
import base64
import json
import struct
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from bilibili_live_monitor.assistant import Actions, AssistantStore, Conflict
from bilibili_live_monitor.assistant_worker import AssistantWorker, WorkerConfig
from bilibili_live_monitor.danmaku import chat_message, entry_message, gift_message, messages
from bilibili_live_monitor.gift_protocol import Gift
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


def lamp(source, at, *, v2=False, generation='current', room='123456', **overrides):
    data = {'uid': 111, 'uname': '用户A', 'giftId': 34001, 'giftName': '粉丝团灯牌',
            'num': 1, 'timestamp': at, 'tid': str(at), **overrides}
    if v2:
        event = Gift(uid=data['uid'], uname=data['uname'])
        event.items.add(gift_id=data['giftId'], gift_name=data['giftName'], num=data['num'],
                        timestamp=int(at), tid=data['tid'])
        if data.get('blind_gift'):
            event.blind_gift.original_gift_id = 1
            event.blind_gift.original_gift_name = '合成盲盒'
        raw = json.dumps({'cmd': 'SEND_GIFT_V2', 'data': {
            'pb': base64.b64encode(event.SerializeToString()).decode()}}).encode()
        message = messages(struct.pack('>IHHII', len(raw)+16, 16, 1, 5, 1)+raw)[0]
    else:
        message = gift_message({'cmd': 'SEND_GIFT', 'data': data})
    source.live_message(room, {**message, 'session_id': generation, 'received_utc': stamp(at)})
    return message


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
    lamp(source, now-15)
    chat(source, now-10)
    worker = AssistantWorker(state, source.path, 'http://synthetic')
    worker.ingest()
    assert state.card('medal:gift:111')['eligible']
    purchase(source, now-5)
    worker.ingest()
    card = state.card('medal:gift:111')
    assert not card['eligible']
    assert 'guard_observation' not in card
    assert any(state.evidence(r['id'])['data'].get('guard_source') == 'purchase' for r in card['evidence'])


def test_roster_selection_and_guard_priority_use_observed_time_not_ingest_order(scene):
    now, source, state = scene
    lamp(source, now-35)
    chat(source, now-30)
    purchase(source, now-20)
    # 后收到的旧弹幕不能把刚观察到的 guard=3 覆盖为0。
    chat(source, now-25, text='乱序旧弹幕')
    roster(source, now-10, members=[111])
    roster(source, now-40)  # id 更大，但观测更旧的完整空榜。
    worker = AssistantWorker(state, source.path, 'http://synthetic')
    worker.ingest()
    # 用更新榜单移除资格后，旧榜单不能重现它。
    assert state.card('medal:gift:111') is None
    roster(source, now-5)  # 更新的完整榜单可提供新的明确缺席证据。
    worker.ingest()
    assert state.card('medal:gift:111')['eligible']
    assert state.card('medal:gift:111')['guard_observation']['observed_utc'] == stamp(now-5)


@pytest.mark.parametrize('guard', [None, 0])
def test_partial_roster_absence_does_not_prove_non_guard(scene, guard):
    now, source, state = scene
    lamp(source, now-15)
    chat(source, now-10, guard=guard)
    roster(source, now-5, complete=False)
    AssistantWorker(state, source.path, 'http://synthetic').ingest()
    assert bool(state.card('medal:gift:111')) is (guard == 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['done', 'dismissed', 'pending'])
async def test_ai_medal_status_uses_voice_evidence_and_preserves_qualification(scene, status):
    now, source, state = scene
    lamp(source, now-15)
    chat(source, now-10)
    source.segment({'room_id': '123456', 'seq': 1, 'session_id': 'current', 'text': '用户A，你的点歌跟进已经完成了',
                    'end_utc': stamp(now-5), 'received_utc': stamp(now-5), 'is_final': True, 'status': 'done'})
    runtime = AsyncMock()
    async with client(now) as transport:
        worker = AssistantWorker(state, source.path, 'http://synthetic', WorkerConfig(interval=0), runtime, transport)
        state.set_enabled(True)
        worker.ingest()
        card = state.card('medal:gift:111')
        qualification = {k: card.get(k) for k in ('eligible', 'uid', 'author', 'title', 'guard_observation')}
        context, target, offered = worker.context()
        assert 'medal:gift:111' in offered['cards']
        refs = offered['evidence']
        assert any(e['data']['kind'] == 'transcript' for e in context['evidence'])
        payload = {'actions': [{'room_id': '123456', 'session_id': 'session', 'card_id': 'medal:gift:111',
                               'kind': 'medal', 'expected_version': card['version'], 'status': status,
                               'title': '不能用AI改写真实资格', 'evidence': refs}]}
        assert 'medal' in Actions.model_json_schema()['$defs']['Action']['properties']['kind']['enum']
        with pytest.raises(Conflict):
            state.apply({'actions': [{**payload['actions'][0], 'evidence': [
                {**r, 'version': r['version']+1} for r in refs]}]}, target, offered)
        assert state.card('medal:gift:111') == card
        runtime.infer.return_value = payload
        await worker.tick()
        updated = state.card('medal:gift:111')
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
    state.set_enabled(True)
    state.apply({'actions': []}, target, offered)
    report = state.state()['worker']
    assert report['pending_observations'] == 0
    assert report['last_analysis'] is not None


def test_high_badge_and_complete_roster_without_gift_never_qualify(scene):
    now, source, state = scene
    message = chat_message([[0, 0, 0, 0, now-20], '用户A的问题', [111, '用户A'],
                            [99, '高等级粉丝牌', 0, 123456], 0, 0, 0, 0])
    source.live_message('123456', {**message, 'session_id': 'current', 'received_utc': stamp(now-20)})
    roster(source, now-10)
    AssistantWorker(state, source.path, 'http://synthetic').ingest()
    assert not any(c['kind'] == 'medal' for c in state.cards())


@pytest.mark.parametrize('values', [
    {'giftId': 1, 'giftName': '普通礼物'},
    {'giftId': 2, 'giftName': '粉丝团灯牌'},
    {'giftId': 34001, 'giftName': '黑猫提灯'},
    {'giftId': 3, 'giftName': '假面', 'blind_gift': {'original_gift_id': 34001, 'original_gift_name': '粉丝团灯牌'}},
    {'blind_gift': {'original_gift_id': 3, 'original_gift_name': '假面'}},
])
@pytest.mark.parametrize('v2', [False, True])
def test_only_exact_non_blind_target_gift_can_create_followup(scene, values, v2):
    now, source, state = scene
    chat(source, now-20, text='粉丝团灯牌 黑猫提灯')
    lamp(source, now-10, v2=v2, **values)
    AssistantWorker(state, source.path, 'http://synthetic').ingest()
    assert not any(c['kind'] == 'medal' for c in state.cards())


@pytest.mark.parametrize('v2', [False, True])
def test_target_gift_dedup_metadata_and_subsequent_quantity(scene, v2):
    now, source, state = scene
    chat(source, now-30)
    message = lamp(source, now-20, v2=v2)
    source.live_message('123456', {**message, 'session_id': 'current', 'received_utc': stamp(now-20)})
    worker = AssistantWorker(state, source.path, 'http://synthetic')
    worker.ingest()
    worker.ingest()
    cards = [c for c in state.cards() if c['kind'] == 'medal']
    assert len(cards) == 1
    card = cards[0]
    assert card['eligible'] and card['gift_quantity'] == 1
    assert card['first_gift_at'] == stamp(now-20)
    assert len(card['gift_evidence']) == 1
    assert state.evidence(card['gift_evidence'][0]['id'])['data']['gift']['gift_id'] == '34001'
    assert state.state()['cards'][-1]['author'] == '用户A'
    assert state.state()['cards'][-1]['pronunciation']['text']
    lamp(source, now-10, v2=v2, num=2)
    worker.ingest()
    updated = state.card(card['id'])
    assert updated['gift_quantity'] == 3 and len(updated['gift_evidence']) == 2
    assert updated['first_gift_at'] == stamp(now-20) and updated['last_gift_at'] == stamp(now-10)


@pytest.mark.parametrize('via_entry', [False, True])
def test_unknown_guard_gift_waits_for_explicit_same_uid_identity(scene, via_entry):
    now, source, state = scene
    lamp(source, now-20)
    worker = AssistantWorker(state, source.path, 'http://synthetic')
    worker.ingest()
    assert state.card('medal:gift:111') is None
    if via_entry:
        message = entry_message({'cmd': 'INTERACT_WORD', 'data': {
            'msg_type': 1, 'uid': 111, 'uname': '用户A', 'guard_level': 0}}, received_utc=stamp(now-10))
        source.live_message('123456', {**message, 'session_id': 'current', 'received_utc': stamp(now-10)})
    else:
        chat(source, now-10)
    worker.ingest()
    assert state.card('medal:gift:111')['eligible']


@pytest.mark.parametrize('guard', [1, 2, 3])
def test_guard_gift_sender_excluded(scene, guard):
    now, source, state = scene
    lamp(source, now-20, guard_level=guard)
    AssistantWorker(state, source.path, 'http://synthetic').ingest()
    assert state.card('medal:gift:111') is None


@pytest.mark.parametrize('values', [{'uid': 0}, {'is_mystery': True}])
def test_unknown_sender_not_matched_by_nickname(scene, values):
    now, source, state = scene
    lamp(source, now-20, **values)
    chat(source, now-10)
    AssistantWorker(state, source.path, 'http://synthetic').ingest()
    assert state.card('medal:gift:111') is None


@pytest.mark.parametrize('scope', ['before_since', 'old_source', 'other_room'])
def test_gift_must_belong_to_fixed_scope(scene, scope):
    now, source, state = scene
    lamp(source, now-70 if scope == 'before_since' else now-20,
         generation='old' if scope == 'old_source' else 'current',
         room='654321' if scope == 'other_room' else '123456')
    chat(source, now-10)
    AssistantWorker(state, source.path, 'http://synthetic').ingest()
    assert state.card('medal:gift:111') is None


@pytest.mark.parametrize('status', ['done', 'dismissed'])
def test_manual_followup_status_not_resurrected_by_rederive_or_new_gift(scene, status):
    now, source, state = scene
    lamp(source, now-20, guard_level=0)
    worker = AssistantWorker(state, source.path, 'http://synthetic')
    worker.ingest()
    card = state.card('medal:gift:111')
    state.manual('session', card['id'], card['version'], status)
    lamp(source, now-10, guard_level=0)
    worker.ingest()
    worker.ingest()
    card = state.card(card['id'])
    assert card['status'] == status and card['manual']
    assert card['gift_quantity'] == 2


def test_legacy_badge_card_hidden_without_deleting_history(scene):
    now, source, state = scene
    chat(source, now-20)
    worker = AssistantWorker(state, source.path, 'http://synthetic')
    worker.ingest()
    ref = {'id': str(source.items('123456')[0]['id']), 'version': 1}
    legacy = {'id': 'medal:111', 'kind': 'medal', 'uid': '111', 'author': '用户A',
              'title': '尚未观察到点歌', 'status': 'done', 'manual': True,
              'version': 1, 'eligible': True, 'evidence': [ref]}
    with state.db:
        state.save_card(legacy)
    worker.ingest()
    assert state.card('medal:111') == {**legacy, 'eligible': False, 'version': 2}
    before = state.db.execute("SELECT after_data FROM history WHERE card_id='medal:111' ORDER BY seq").fetchall()
    assert json.loads(before[0][0]) == legacy and len(before) == 2
    lamp(source, now-10, guard_level=0)
    worker.ingest()
    assert not state.card('medal:111')['eligible']
    assert state.card('medal:111')['status'] == 'done'
    assert state.card('medal:gift:111')['eligible']
    assert state.card('medal:gift:111')['gift_evidence']


@pytest.mark.parametrize('status, eligible', [('pending', True), ('queued', True), ('sung', False), ('rejected', False)])
def test_song_followup_completion_is_not_merely_observing_request(scene, status, eligible):
    now, source, state = scene
    lamp(source, now-20, guard_level=0)
    chat(source, now-10, text='想听歌曲A')
    worker = AssistantWorker(state, source.path, 'http://synthetic')
    worker.ingest()
    chat_card = next(c for c in state.cards() if c['kind'] == 'chat')
    _, target, offered = worker.context()
    state.set_enabled(True)
    state.apply({'actions': [{'room_id': '123456', 'session_id': 'session', 'card_id': 'song:request',
                             'kind': 'song', 'expected_version': 0, 'status': status, 'title': '歌曲A',
                             'evidence': chat_card['evidence']}]}, target, offered)
    worker.ingest()
    assert state.card('medal:gift:111')['eligible'] is eligible


@pytest.mark.asyncio
@pytest.mark.parametrize('gift_count', [12, 16])
async def test_many_gifts_use_bounded_witness_without_losing_audit_or_manual_lock(scene, gift_count):
    now, source, state = scene
    chat(source, now-40)
    for i in range(gift_count):
        lamp(source, now-35+i, tid=f'gift-{i}')

    def speech(seq):
        source.segment({'room_id': '123456', 'seq': seq, 'session_id': 'current',
                        'text': f'用户A的点歌跟进完成，确认{seq}', 'end_utc': stamp(now+seq),
                        'received_utc': stamp(now+seq), 'is_final': True, 'status': 'done'})

    speech(1)
    runtime = AsyncMock()
    async def infer(context):
        card = next(c for c in context['cards'] if c['id'] == 'medal:gift:111')
        assert 'gift_evidence' not in card
        assert card['gift_receipt_count'] >= gift_count
        assert len(card['qualification_evidence']) <= 3
        assert len(card['evidence']) <= 12
        voice = max((e for e in context['evidence'] if e['data']['kind'] == 'transcript'),
                    key=lambda e: e['data']['seq'])
        refs = card['qualification_evidence'] + [{'id': voice['id'], 'version': voice['version']}]
        assert all(any(e['id'] == r['id'] and e['version'] == r['version'] for e in context['evidence']) for r in refs)
        payload = {'actions': [{'room_id': '123456', 'session_id': 'session', 'card_id': card['id'],
                               'kind': 'medal', 'expected_version': card['version'], 'status': 'done',
                               'title': card['title'], 'evidence': refs}]}
        Actions.model_validate(payload)
        return payload
    runtime.infer.side_effect = infer
    async with client(now) as transport:
        worker = AssistantWorker(state, source.path, 'http://synthetic', WorkerConfig(interval=0), runtime, transport)
        state.set_enabled(True)
        if gift_count == 16:
            worker.ingest()
            legacy = state.card('medal:gift:111')
            full = legacy['gift_evidence'] + [r for r in legacy['evidence'] if r not in legacy['gift_evidence']]
            legacy.pop('qualification_evidence')
            with state.db:
                state.save_card({**legacy, 'evidence': full, 'status_evidence': full})
        await worker.tick()
        card = state.card('medal:gift:111')
        assert card['status'] == 'done' and state.get('last_error') is None
        assert len(card['gift_evidence']) == card['gift_quantity'] == gift_count
        assert card['first_gift_at'] == stamp(now-35)
        assert card['last_gift_at'] == stamp(now-35+gift_count-1)
        first_voice = next(r for r in card['status_evidence'] if state.evidence(r['id'])['data']['kind'] == 'transcript')
        # 多次新的状态判断不能累积成无法再更新的卡片；早期证据仍在审计历史里。
        for seq in range(2, 13):
            lamp(source, now+seq-.5, tid=f'later-{seq}')
            speech(seq)
            await worker.tick()
            card = state.card(card['id'])
            assert card['status'] == 'done' and not card['manual']
            assert len(card['evidence']) <= 12 and len(card['status_evidence']) <= 9
            assert card['gift_quantity'] == gift_count+seq-1
            assert card['first_gift_at'] == stamp(now-35)
            assert card['last_gift_at'] == stamp(now+seq-.5)
            assert state.get('last_error') is None
            if seq == 2:
                assert first_voice in card['status_evidence']
        assert state.evidence(first_voice['id']) is not None
        assert any(first_voice in json.loads(r[0]).get('status_evidence', []) for r in state.db.execute(
            'SELECT after_data FROM history WHERE card_id=?', (card['id'],)))
        report = state.state()
        assert all(any(e['id'] == r['id'] for e in report['evidence']) for r in card['gift_evidence'])
        state.manual('session', card['id'], card['version'], 'done')
        lamp(source, now+14, tid='after-manual')
        speech(14)
        await worker.tick()
        card = state.card(card['id'])
        assert card['manual'] and card['status'] == 'done'
        assert state.get('last_error') == 'Conflict'
        assert len(card['gift_evidence']) == card['gift_quantity'] == gift_count+12
        assert len(card['evidence']) <= 12
        assert state.db.execute("SELECT count(*) FROM observations WHERE json_extract(data,'$.kind')='gift'").fetchone()[0] == gift_count+12
