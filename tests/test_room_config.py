import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from bilibili_live_monitor.app import create_app
from bilibili_live_monitor.automation import Automation
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.room_config import reload_plan
from bilibili_live_monitor.store import Store


def make(tmp_path, rooms=None):
    store = Store(tmp_path/'monitor.sqlite3')
    platform = SimpleNamespace(store=store, monitor=SimpleNamespace(browser=None))
    return Automation(platform, SimpleNamespace(), {'rooms': rooms or {}})


def test_new_room_defaults_off_and_persisted_switch_survives_restart(tmp_path):
    config = {'123456': {'label': '测试', 'intro': '欢迎'}}
    service = make(tmp_path, config)
    assert service.row('123456')['enabled'] is False
    service.row('123456').update(enabled=True, seen=['viewer'], last_sent=123)
    service.save()
    restored = Automation(service.platform, service.accounts, service.config)
    assert restored.row('123456')['enabled'] is True
    assert restored.row('123456')['seen'] == ['viewer']


@pytest.mark.asyncio
async def test_add_and_reload_preserve_session_and_do_not_duplicate_loop(tmp_path):
    service = make(tmp_path, {'123456': {'label': '旧', 'intro': '欢迎', 'welcome_enabled': True}})
    # A real blocking coroutine lets us check loop ownership.
    async def run():
        await asyncio.Event().wait()
    service.run = run
    service.row('123456').update(seen=['old'], session='old-session', last_sent=123)
    service.workflows.records['old'] = {'url': 'https://example.org/old/'}
    before = deepcopy(service.row('123456'))
    incoming = deepcopy(service.config)
    incoming['rooms']['654321'] = {'label': '新', 'intro': '欢迎'}
    result = await service.reload_rooms(incoming)
    loop = service.loop
    await service.reload_rooms(incoming)
    assert result['added'] == ['654321']
    assert service.loop is loop
    assert service.row('654321')['enabled'] is False
    assert service.row('123456') == before
    assert service.workflows.records['old']['url'].endswith('/old/')
    await service.close()


@pytest.mark.asyncio
async def test_explicit_disable_revokes_inflight_send(tmp_path):
    service = make(tmp_path, {'123456': {'label': '旧', 'intro': '欢迎', 'welcome_enabled': True}})
    service.start = AsyncMock()
    service.row('123456')
    task = asyncio.create_task(asyncio.Event().wait())
    service.tasks['welcome:123456'] = task
    incoming = deepcopy(service.config)
    incoming['rooms']['123456']['welcome_enabled'] = False
    await service.reload_rooms(incoming)
    assert task.cancelled()
    assert service.row('123456')['enabled'] is False
    assert service.row('123456')['revision'] == 1


@pytest.mark.asyncio
async def test_invalid_reload_changes_nothing(tmp_path):
    service = make(tmp_path, {'123456': {'label': '旧', 'intro': '欢迎'}})
    before = deepcopy(service.config)
    for mutate in (
        lambda c: c['rooms'].clear(),
        lambda c: c['rooms']['123456'].update(welcome_enabled='false'),
        lambda c: c.update(notification_command=['changed']),
        lambda c: c.update(session_workflow={'enabled': True}),
        lambda c: c['rooms']['123456'].update(greeting='{bad}{names}'),
    ):
        incoming = deepcopy(before)
        mutate(incoming)
        with pytest.raises(ValueError):
            await service.reload_rooms(incoming)
        assert service.config == before
        assert service.rooms == before['rooms']


@pytest.mark.asyncio
async def test_reload_updates_variants_and_preserves_saved_copy(tmp_path):
    service = make(tmp_path, {'123456': {'label': '旧', 'intro': '欢迎'}})
    service.accounts.summary = lambda: {}
    service.start = AsyncMock()
    incoming = deepcopy(service.config)
    incoming['rooms']['654321'] = {
        'label': '新', 'intro': '介绍',
        'welcome_variants': [{'id': 'first', 'info': '新介绍', 'cta': '新引导'}]}
    await service.reload_rooms(incoming)
    assert service.summary()['rooms'][1]['variant_count'] == 1
    assert service.variants['654321'][0]['id'] == 'first'
    await service.update_welcome_copy('654321', {
        **service.welcome_copy('654321'), 'intro': '界面保存', 'welcome_variants': []})
    await service.reload_rooms(incoming)
    assert service.welcome_copy('654321')['intro'] == '界面保存'
    assert service.variants['654321'] == []
    before = deepcopy(service.rooms)
    broken = deepcopy(incoming)
    broken['rooms']['123456']['welcome_variants'] = [{'id': 'bad'}]
    with pytest.raises(ValueError):
        await service.reload_rooms(broken)
    assert service.rooms == before
    service.store.close()


def test_only_future_publish_mapping_can_change():
    config = {'rooms': {}, 'session_workflow': {'enabled': False, 'publish': {'default': {}}}}
    incoming = deepcopy(config)
    incoming['session_workflow']['publish']['default']['url'] = 'https://example.org/new/'
    assert reload_plan(config, incoming) == incoming
    assert config['session_workflow']['publish']['default'] == {}


def test_reload_api_uses_fixed_local_file(tmp_path):
    path = tmp_path/'automation.json'
    path.write_text('{"rooms":{}}')
    app = create_app(Settings(data_dir=tmp_path, automation_config=str(path)))
    app.state.automation = SimpleNamespace(reload_rooms=AsyncMock(return_value={'accepted': True}))
    client = TestClient(app)
    assert client.post('/api/automation/reload').json() == {'accepted': True}
    path.write_text('{broken')
    assert client.post('/api/automation/reload').status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize('publish', [None, {'rooms': []}, {'default': []},
    {'rooms': {'123456': None}}, {'default': {'url': 123}}, {'default': {'remote': None}}])
async def test_nested_publish_types_fail_before_state_change(tmp_path, publish):
    service = make(tmp_path, {'123456': {'label': '测试', 'intro': '欢迎'}})
    service.config['session_workflow'] = {'enabled': False, 'publish': {}}
    before = deepcopy(service.config)
    incoming = deepcopy(before)
    incoming['session_workflow']['publish'] = publish
    with pytest.raises(ValueError):
        await service.reload_rooms(incoming)
    assert service.config == before
    assert service.rooms == before['rooms']
