import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bilibili_live_monitor.accounts import Accounts
from bilibili_live_monitor.audience_crm import identity
from bilibili_live_monitor.identity import analysis_key
from bilibili_live_monitor.store import Store


def test_anonymous_storage_preserves_protocol_uid_and_groups_names(tmp_path):
    store = Store(tmp_path / 'monitor.sqlite3')
    store.nickname_identity = True
    for index, uid in enumerate((None, '123')):
        store.live_message('456', {'kind': 'entry', 'author': '名字', 'sender_uid': uid,
                                 'identity_status': 'uid' if uid else 'unresolved',
                                 'source_key': str(index), 'at_utc': '2030-01-01T00:00:00Z'})
    rows = store.items('456')
    assert [row['sender_uid'] for row in rows] == [None, '123']
    assert identity(rows[0]) == identity(rows[1])
    assert identity(rows[0]).startswith('nickname:')
    assert identity({**rows[0], 'room_id': '789'}) != identity(rows[0])
    store.close()


def test_regular_missing_uid_stays_unresolved_and_mystery_is_not_grouped():
    data = {'room_id': '456', 'author': '名字', 'identity_status': 'unresolved'}
    assert analysis_key(data) is None
    assert identity(data) is None
    assert analysis_key({**data, 'analysis_identity_basis': 'nickname', 'author': '神秘人'}) is None
    assert analysis_key({**data, 'analysis_identity_basis': 'nickname', 'room_id': None}) is None


@pytest.mark.asyncio
async def test_anonymous_cannot_open_login_or_continue_logged_in(tmp_path):
    accounts = Accounts(SimpleNamespace(data_dir=tmp_path), {
        'profiles': {'anonymous': {'mode': 'anonymous', 'label': '匿名', 'path': str(tmp_path/'profile')}},
        'active_profile': 'anonymous'})
    browser = SimpleNamespace(connect=AsyncMock())
    with pytest.raises(ValueError, match='不支持登录'):
        await accounts.login(browser)
    browser.connect.assert_not_awaited()
    response = SimpleNamespace(status=200, json=AsyncMock(return_value={
        'code': 0, 'data': {'isLogin': True, 'mid': 123}}))
    browser.connect.return_value = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
    accounts.close = AsyncMock()
    with pytest.raises(RuntimeError, match='意外登录'):
        await accounts.refresh(browser)
    accounts.close.assert_awaited_once()
    assert not accounts.identity['logged_in']


def test_profile_mode_persisted_selection(tmp_path):
    (tmp_path / 'active_account.json').write_text(json.dumps({'active': 'anonymous'}))
    accounts = Accounts(SimpleNamespace(data_dir=tmp_path), {
        'profiles': {'anonymous': {'mode': 'anonymous', 'label': '匿名', 'path': str(tmp_path/'profile')}},
        'active_profile': 'other'})
    assert accounts.summary()['mode'] == 'anonymous'


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{'code': 0, 'data': {}}, {'code': -352}, []])
async def test_incomplete_guest_response_closes_browser(tmp_path, payload):
    accounts = Accounts(SimpleNamespace(data_dir=tmp_path), {
        'profiles': {'anon': {'mode': 'anonymous'}}, 'active_profile': 'anon'})
    response = SimpleNamespace(status=200, json=AsyncMock(return_value=payload))
    browser = SimpleNamespace(connect=AsyncMock(return_value=SimpleNamespace(
        request=SimpleNamespace(get=AsyncMock(return_value=response)))))
    accounts.close = AsyncMock()
    with pytest.raises((RuntimeError, AttributeError)):
        await accounts.refresh(browser)
    accounts.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_guest_network_failure_closes_browser(tmp_path):
    accounts = Accounts(SimpleNamespace(data_dir=tmp_path), {
        'profiles': {'anon': {'mode': 'anonymous'}}, 'active_profile': 'anon'})
    browser = SimpleNamespace(connect=AsyncMock(side_effect=TimeoutError))
    accounts.close = AsyncMock()
    with pytest.raises(TimeoutError):
        await accounts.refresh(browser)
    accounts.close.assert_awaited_once()


def test_mystery_gift_cannot_be_joined_by_display_name():
    from bilibili_live_monitor.danmaku import gift_message
    item = gift_message({'cmd': 'SEND_GIFT', 'data': {
        'uid': 123, 'uname': '隐藏昵称', 'is_mystery': True,
        'giftId': 1, 'giftName': '合成礼物', 'num': 1}})
    item.update(room_id='456', analysis_identity_basis='nickname')
    assert item['sender_hidden'] and item['sender_uid'] is None
    assert analysis_key(item) is None


def test_nickname_crm_preserves_raw_uid_redaction(tmp_path):
    from test_audience_crm import config, session

    from bilibili_live_monitor.audience_crm import collect
    from bilibili_live_monitor.audience_pages import public_text
    source = session()
    for _, _, data in source['source']['items']:
        data.update(room_id='10001', analysis_identity_basis='nickname')
    material = collect(config(tmp_path), [source])
    assert len(material['people']) == 1
    person = next(iter(material['people'].values()))
    assert person['uid'] is None and person['identity_basis'] == 'nickname'
    assert set(person['observed_uids']) == {'101', '102'}
    assert public_text('数字 101', set(person['observed_uids'])) == '数字 [账号已隐藏]'


@pytest.mark.asyncio
@pytest.mark.parametrize('source,target', [('account', 'anonymous'), ('anonymous', 'account')])
async def test_failed_switch_keeps_suspended_and_target_identity_mode(tmp_path, source, target):
    import asyncio

    from test_persistent_automation import make
    service, _ = make(tmp_path)
    platform = service.platform
    platform.reconcile_lock = asyncio.Lock()
    platform.revision = 0
    platform.save = lambda: None
    platform.status_task = platform.guard_task = None
    platform.launching, platform.active, platform.monitor.tasks = {}, {}, {}
    service.accounts.profiles = {'first': {'mode': source}, 'second': {'mode': target}}
    service.accounts.close = service.accounts.start = AsyncMock()
    service.accounts.refresh = AsyncMock(side_effect=RuntimeError('synthetic failure'))
    service.browser = SimpleNamespace(close=AsyncMock())
    with pytest.raises(RuntimeError):
        await service.switch_account('second')
    assert platform.suspended
    assert platform.store.nickname_identity == (target == 'anonymous')


@pytest.mark.asyncio
async def test_recovery_drains_capture_before_starting_unverified_browser(tmp_path):
    import asyncio

    from test_persistent_automation import make

    service, _ = make(tmp_path)
    platform = service.platform
    platform.reconcile_lock = asyncio.Lock()
    platform.active = {'123456': True}
    platform.launching = {}
    platform.monitor.tasks = {'123456': True}
    order = []
    ready = asyncio.Event()

    async def pending_launch():
        try:
            await asyncio.Event().wait()
        finally:
            order.append('cancel_launch')

    launch = asyncio.create_task(pending_launch())
    platform.launching['pending'] = launch
    await asyncio.sleep(0)

    async def stop(rid):
        order.append('stop')
        platform.monitor.tasks.pop(rid)

    async def start():
        assert not platform.monitor.tasks and not platform.active and not platform.launching
        assert platform.suspended and platform.reconcile_lock.locked()
        order.append('start')
        ready.set()
        raise RuntimeError('synthetic verification failure')

    platform.monitor.stop = stop
    service.accounts.profiles = {'first': {'mode': 'anonymous'}}
    service.accounts.process = None
    service.accounts.close = AsyncMock()
    service.accounts.start = start
    service.browser = SimpleNamespace(close=AsyncMock())
    runner = asyncio.create_task(service.run())
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert order == ['cancel_launch', 'stop', 'start']
        assert platform.suspended
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


def test_local_only_publish_and_verification_make_no_external_calls(monkeypatch):
    from bilibili_live_monitor import live_share_watch, session_runner

    def forbidden(*args, **kwargs):
        pytest.fail('local-only monitoring must not publish or fetch a remote page')

    monkeypatch.setattr(live_share_watch.subprocess, 'run', forbidden)
    monkeypatch.setattr(session_runner.urllib.request, 'urlopen', forbidden)
    live_share_watch.publish({'local_only': True}, None)
    session_runner.verify_remote({'local_only': True})
