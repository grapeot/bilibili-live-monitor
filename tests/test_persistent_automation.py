import asyncio
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from bilibili_live_monitor.app import LikeRequest, create_app
from bilibili_live_monitor.automation import Automation, confirmed_state
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.likes import LIKE_STEP, trigger_likes
from bilibili_live_monitor.store import Store
from bilibili_live_monitor.welcome import text_length, welcome_messages


def make(tmp_path):
    store = Store(tmp_path/'monitor.sqlite3')
    candidate = {'url': 'https://live.bilibili.com/123456', 'room_id': '123456',
                 'status_checked_utc': datetime.now(timezone.utc).isoformat(),
                 'live_status': 1, 'offline_checks': 0, 'blocked': False}
    platform = SimpleNamespace(store=store, monitor=SimpleNamespace(browser=None),
        policy=SimpleNamespace(enabled=True, mode='fixed'),
        candidates={candidate['url']: candidate}, fixed_urls=lambda: {candidate['url']})
    accounts = SimpleNamespace(epoch=1, switching=False, active='first', identity={'logged_in': True},
                               summary=lambda: {})
    service = Automation(platform, accounts, {'rooms': {'123456': {'label': '合成主播', 'intro': '欢迎听歌。'}},
                                             'notification_command': ['synthetic-mail']})
    return service, candidate


def test_transition_persists_across_restart_and_never_calls_unknown_offline(tmp_path):
    service, candidate = make(tmp_path)
    service.observe_state('123456')
    session = service.row('123456')['session']
    assert len(service.state['notifications']) == 1
    service.save()
    restarted = Automation(service.platform, service.accounts,
                           {'rooms': service.rooms, 'notification_command': ['synthetic-mail']})
    restarted.observe_state('123456')
    assert restarted.row('123456')['session'] == session
    assert len(restarted.state['notifications']) == 1
    candidate.update(live_status=None, status_error='network')
    restarted.observe_state('123456')
    assert restarted.row('123456')['confirmed'] == 'live'
    candidate.update(live_status=0, status_error=None, offline_checks=1)
    restarted.observe_state('123456')
    assert len(restarted.state['notifications']) == 1
    candidate['offline_checks'] = 2
    restarted.observe_state('123456')
    assert len(restarted.state['notifications']) == 2
    candidate.update(live_status=1, offline_checks=0)
    restarted.observe_state('123456')
    assert len(restarted.state['notifications']) == 3
    assert restarted.row('123456')['session'] != session


def test_initial_offline_is_baseline_and_stale_status_is_ignored(tmp_path):
    service, candidate = make(tmp_path)
    candidate.update(live_status=0, offline_checks=2)
    service.observe_state('123456')
    assert not service.state['notifications']
    candidate['status_checked_utc'] = (datetime.now(timezone.utc)-timedelta(seconds=76)).isoformat()
    assert confirmed_state(candidate) is None


@pytest.mark.asyncio
async def test_toggle_immediate_revoke_persist_seen_and_cooldown(tmp_path):
    service, _ = make(tmp_path)
    row = service.row('123456')
    row.update(seen=['1', '2'], last_sent=time.time(), pending=[(time.time(), '3', 'new')])
    old_revision = row['revision']
    await service.set_enabled('123456', False)
    assert row['revision'] > old_revision and not row['enabled'] and row['pending'] == []
    await service.set_enabled('123456', True)
    assert row['seen'] == ['1', '2'] and row['last_sent'] is not None


def test_unknown_delivery_is_not_replayed_on_restart(tmp_path):
    service, _ = make(tmp_path)
    service.row('123456')['delivery'] = 'sending'
    service.state['notifications']['x'] = {'status': 'sending'}
    service.save()
    restarted = Automation(service.platform, service.accounts, {'rooms': service.rooms})
    assert restarted.row('123456')['delivery'] == 'ambiguous'
    assert restarted.state['notifications']['x']['status'] == 'unknown'


def test_changed_live_start_detects_missed_offline_cycle(tmp_path):
    service, candidate = make(tmp_path)
    candidate['live_start'] = '2030-01-01T00:00:00Z'
    service.observe_state('123456')
    before = service.row('123456')
    before['seen'] = ['old-viewer']
    service.save()
    service = Automation(service.platform, service.accounts,
                         {'rooms': service.rooms, 'notification_command': ['synthetic']})
    candidate['live_start'] = '2030-01-02T00:00:00Z'
    service.observe_state('123456')
    assert service.row('123456')['session'] != before['session']
    assert service.row('123456')['seen'] == []
    assert len(service.state['notifications']) == 2


@pytest.mark.asyncio
async def test_cancel_before_like_task_first_step(tmp_path):
    service, _ = make(tmp_path)
    service.start_likes('123456', 1, 'first')
    result = await service.cancel_likes('first')
    assert result['status'] == 'cancelled'
    assert result['triggered'] == 0


@pytest.mark.asyncio
async def test_account_switch_waits_for_reconciliation(tmp_path):
    service, _ = make(tmp_path)
    platform = service.platform
    platform.reconcile_lock = asyncio.Lock()
    await platform.reconcile_lock.acquire()
    platform.revision = 0
    platform.save = lambda: None
    platform.status_task = platform.guard_task = None
    platform.launching = {}
    platform.active = {}
    platform.monitor.tasks = {'123456': True}
    platform.monitor.stop = AsyncMock()
    service.accounts.profiles = {'second': {}}
    service.accounts.close = AsyncMock()
    service.accounts.start = AsyncMock()
    service.accounts.refresh = AsyncMock()
    service.accounts.login = AsyncMock()
    service.browser = SimpleNamespace(close=AsyncMock())
    task = asyncio.create_task(service.switch_account('second'))
    await asyncio.sleep(0)
    assert platform.suspended and not service.accounts.close.called
    platform.reconcile_lock.release()
    await task
    platform.monitor.stop.assert_awaited_once_with('123456')
    service.accounts.close.assert_awaited_once()
    assert not platform.suspended and not service.accounts.switching


@pytest.mark.asyncio
async def test_pre_send_failure_does_not_poison_broadcast(tmp_path):
    service, _ = make(tmp_path)
    service.observe_state('123456')
    service.row('123456')['pending'] = [(time.time(), '1', 'A'), (time.time(), '2', 'B')]
    service.page = AsyncMock(side_effect=RuntimeError('not ready'))
    await service.deliver_welcome('123456')
    assert service.row('123456')['delivery'] == 'paused'


@pytest.mark.asyncio
async def test_profile_switch_cancels_not_started_likes(tmp_path):
    service, _ = make(tmp_path)
    p = service.platform
    p.reconcile_lock, p.revision = asyncio.Lock(), 0
    p.save = lambda: None
    p.status_task = p.guard_task = None
    p.launching, p.active, p.monitor.tasks = {}, {}, {}
    service.accounts.profiles = {'second': {}}
    for method in ('close', 'start', 'refresh', 'login'):
        setattr(service.accounts, method, AsyncMock())
    service.browser = SimpleNamespace(close=AsyncMock())
    service.start_likes('123456', 2, 'not-started')
    await service.switch_account('second')
    assert service.state['likes']['not-started']['status'] == 'cancelled'
    service.state['likes']['finished'] = {'id': 'finished', 'room_id': '123456', 'requested': 1,
                                        'triggered': 1, 'status': 'completed'}
    await service.cancel_likes('finished')
    assert service.state['likes']['finished']['status'] == 'completed'


@pytest.mark.asyncio
async def test_browser_recovery_and_account_switch_are_serialized(tmp_path):
    service, _ = make(tmp_path)
    p = service.platform
    p.reconcile_lock, p.revision = asyncio.Lock(), 0
    p.save = lambda: None
    p.status_task = p.guard_task = None
    p.launching, p.active, p.monitor.tasks = {}, {}, {}
    service.accounts.profiles = {'first': {}, 'second': {}}
    service.accounts.process = SimpleNamespace(poll=lambda: 1)
    entered, release = asyncio.Event(), asyncio.Event()
    closes, starts = [], []
    async def close():
        closes.append(service.accounts.active)
        entered.set()
        await release.wait()
    async def start():
        starts.append(service.accounts.active)
        service.accounts.process = SimpleNamespace(poll=lambda: None)
    service.accounts.close, service.accounts.start = close, start
    service.accounts.refresh = service.accounts.login = AsyncMock()
    service.browser = SimpleNamespace(close=AsyncMock())
    runner = asyncio.create_task(service.run())
    service.loop = runner
    await asyncio.wait_for(entered.wait(), 1)
    switch = asyncio.create_task(service.switch_account('second'))
    await asyncio.sleep(0)
    assert closes == ['first']
    release.set()
    await switch
    assert starts == ['first', 'second']
    await service.close()


@pytest.mark.asyncio
async def test_likes_deduplicate_request_and_cancel(tmp_path, monkeypatch):
    service, _ = make(tmp_path)
    service.page = AsyncMock(return_value=SimpleNamespace(url='https://live.bilibili.com/123456'))
    async def blocked(*args):
        await __import__('asyncio').Event().wait()
    monkeypatch.setattr('bilibili_live_monitor.automation.trigger_likes', blocked)
    first = service.start_likes('123456', 20, 'request-1')
    assert service.start_likes('123456', 20, 'request-1') is first
    with pytest.raises(ValueError):
        service.start_likes('123456', 21, 'request-1')
    await __import__('asyncio').sleep(0)
    result = await service.cancel_likes('request-1')
    assert result['status'] == 'cancelled' and result['triggered'] == 0


@pytest.mark.asyncio
async def test_like_loop_checks_authority_before_every_action(monkeypatch):
    page = SimpleNamespace(url='https://live.bilibili.com/123456',
                           evaluate=AsyncMock(return_value={'triggered': True, 'delay': 500}))
    valid = iter([True, False])
    progress = []
    monkeypatch.setattr('bilibili_live_monitor.likes.asyncio.sleep', AsyncMock())
    with pytest.raises(RuntimeError):
        await trigger_likes(page, '123456', 10, lambda: next(valid), progress.append)
    assert progress == [1] and page.evaluate.await_count == 1


def test_reused_like_javascript_preserves_website_cooldown():
    script = f'''let n=0; const vm={{loginStatus:{{isLogin:true}},isShowLikeBtn:true,
      likeBtnClickFrequencyLimit:.35,likeBtnClickIsCoolingDown:false,
      handleLikeBtnClick(){{n++;this.likeBtnClickIsCoolingDown=true}}}};
      global.document={{querySelector:()=>({{__vue__:vm}})}};
      const step=({LIKE_STEP});(async()=>console.log(JSON.stringify([await step(),await step(),n])))();'''
    result = json.loads(subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True).stdout)
    assert result == [{'triggered': True, 'delay': 450}, {'triggered': False, 'delay': 450}, 1]


def test_room_specific_copy_splits_without_losing_text():
    intro = '合成主播唱古风、流行和戏腔。首次送灯牌免费点歌，舰长包月点歌。'
    messages = welcome_messages(['访客'], 36, intro=intro, greeting_template='欢迎{names}～')
    assert ''.join(messages[1:]) == intro
    assert all(text_length(message) <= 36 for message in messages)
    assert messages[0] == '欢迎访客～'


@pytest.mark.parametrize('count', [0, 1001, True, 1.5, '2'])
def test_likes_count_is_explicit_bounded_integer(count):
    with pytest.raises(ValueError):
        LikeRequest(count=count, request_id='abc')


def test_http_controls_and_disabled_state_survive_restart(tmp_path, monkeypatch):
    config = tmp_path/'automation.json'
    config.write_text(json.dumps({'rooms': {'123456': {'label': '合成主播', 'intro': '合成欢迎'}}}))
    monkeypatch.setattr('bilibili_live_monitor.accounts.Accounts.refresh', AsyncMock())
    settings = Settings(data_dir=tmp_path, automation_config=str(config))
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/automation').json()['rooms'][0]['enabled'] is True
        response = client.put('/api/automation/rooms/123456', json={'enabled': False})
        assert response.status_code == 200 and not response.json()['rooms'][0]['enabled']
        assert client.put('/api/automation/rooms/999999', json={'enabled': True}).status_code == 404
        assert client.post('/api/automation/account', json={'profile': 'missing'}).status_code == 422
        assert client.post('/api/automation/rooms/123456/likes', json={'count': 1, 'request_id': 'test'}).status_code == 409
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/automation').json()['rooms'][0]['enabled'] is False
