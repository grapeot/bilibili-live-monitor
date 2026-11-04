import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_persistent_automation import make

from bilibili_live_monitor.app import create_app
from bilibili_live_monitor.automation import Automation
from bilibili_live_monitor.config import Settings


def draft(service, info='新的介绍'):
    return {**service.welcome_copy('123456'), 'intro': info,
            'welcome_variants': [{'id': 'new', 'info': info, 'cta': '新的点歌引导'}]}


@pytest.mark.asyncio
async def test_update_persists_without_resetting_live_state(tmp_path):
    service, _ = make(tmp_path)
    service.observe_state('123456')
    service.row('123456').update(last_sent=123, seen=['viewer'], pending=[[456, 'other', '合成访客']])
    service.save()
    before = deepcopy(service.state)
    original = deepcopy(service.rooms)
    result = await service.update_welcome_copy('123456', draft(service))
    assert result['revision'] == 1 and service.state == before
    assert original['123456']['intro'] == '欢迎听歌。'
    restarted = Automation(service.platform, service.accounts, {'rooms': original})
    assert restarted.welcome_copy('123456') == result
    assert restarted.row('123456')['last_sent'] == 123
    service.store.close()


@pytest.mark.asyncio
async def test_inflight_batch_keeps_old_pair_and_next_batch_uses_new(tmp_path, monkeypatch):
    service, _ = make(tmp_path)
    await service.update_welcome_copy('123456', draft(service, '旧的介绍'))
    service.observe_state('123456')
    service.page = AsyncMock(return_value=object())
    monkeypatch.setattr('bilibili_live_monitor.automation.message_limit', AsyncMock(return_value=80))
    monkeypatch.setattr('bilibili_live_monitor.automation.asyncio.sleep', AsyncMock())
    messages = []

    async def send(page, text, **kwargs):
        messages.append(text)
        if len(messages) == 1:
            await service.update_welcome_copy('123456', draft(service, '新的介绍'))
        assert kwargs['allowed']()
        return {'code': 0}

    monkeypatch.setattr('bilibili_live_monitor.automation.send_chat', send)
    service.row('123456')['pending'] = [(0, '1', '甲'), (0, '2', '乙')]
    await service.deliver_welcome('123456')
    assert messages[1] == '旧的介绍'
    service.row('123456')['pending'] = [(0, '3', '丙'), (0, '4', '丁')]
    await service.deliver_welcome('123456')
    assert messages[4] == '新的介绍'
    service.store.close()


@pytest.mark.asyncio
async def test_invalid_stale_or_failed_save_preserves_current_copy(tmp_path, monkeypatch):
    service, _ = make(tmp_path)
    initial = service.welcome_copy('123456')
    with pytest.raises(ValueError):
        await service.update_welcome_copy('123456', {**draft(service), 'greeting': '没有名字占位符'})
    assert service.welcome_copy('123456') == initial
    old = draft(service)
    await service.update_welcome_copy('123456', old)
    with pytest.raises(RuntimeError):
        await service.update_welcome_copy('123456', old)
    current = service.welcome_copy('123456')
    with monkeypatch.context() as m:
        m.setattr(service.store, 'save_control', lambda *a: (_ for _ in ()).throw(OSError('disk unavailable')))
        with pytest.raises(OSError):
            await service.update_welcome_copy('123456', draft(service, '不得生效'))
    assert service.welcome_copy('123456') == current
    service.store.close()


def test_api_validation_conflict_room_isolation_and_restart(tmp_path, monkeypatch):
    import json
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'rooms': {'123456': {'label': '合成甲', 'intro': '甲介绍'},
                                           '234567': {'label': '合成乙', 'intro': '乙介绍'}}}))
    monkeypatch.setattr('bilibili_live_monitor.accounts.Accounts.refresh', AsyncMock())
    settings = Settings(data_dir=tmp_path, automation_config=str(config))
    with TestClient(create_app(settings)) as client:
        path = '/api/automation/rooms/123456/copy'
        body = client.get(path).json()
        body['intro'] = '新的甲介绍'
        assert client.put(path, json={**body, 'enabled': False}).status_code == 422
        assert client.put(path, json={**body, 'greeting': '缺少占位符'}).status_code == 422
        response = client.put(path, json=body)
        assert response.status_code == 200 and response.json()['revision'] == 1
        assert client.put(path, json=body).status_code == 409
        assert client.get('/api/automation/rooms/234567/copy').json()['intro'] == '乙介绍'
        assert client.put('/api/automation/rooms/999999/copy', json=body).status_code == 404
    with TestClient(create_app(settings)) as client:
        assert client.get(path).json()['intro'] == '新的甲介绍'


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['page', 'limit'])
@pytest.mark.parametrize('old_rotates', [True, False])
async def test_update_while_preparing_keeps_whole_batch_snapshot(tmp_path, monkeypatch, stage, old_rotates):
    service, _ = make(tmp_path)
    old = {**draft(service, '旧介绍'), 'greeting': '旧欢迎{names}'}
    if not old_rotates:
        old['welcome_variants'] = []
    await service.update_welcome_copy('123456', old)
    service.observe_state('123456')
    entered, resume = asyncio.Event(), asyncio.Event()

    async def page(_):
        if stage == 'page':
            entered.set()
            await resume.wait()
        return object()

    async def limit(_):
        if stage == 'limit':
            entered.set()
            await resume.wait()
        return 80

    service.page = page
    monkeypatch.setattr('bilibili_live_monitor.automation.message_limit', limit)
    monkeypatch.setattr('bilibili_live_monitor.automation.asyncio.sleep', AsyncMock())
    sender = AsyncMock(return_value={'code': 0})
    monkeypatch.setattr('bilibili_live_monitor.automation.send_chat', sender)
    service.row('123456')['pending'] = [(0, '1', '甲'), (0, '2', '乙')]
    task = asyncio.create_task(service.deliver_welcome('123456'))
    await entered.wait()
    new = {**draft(service, '新介绍'), 'greeting': '新欢迎{names}'}
    if old_rotates:
        new['welcome_variants'] = []
    await service.update_welcome_copy('123456', new)
    resume.set()
    await task
    messages = [call.args[1] for call in sender.await_args_list]
    assert messages == (['旧欢迎甲、乙', '旧介绍', '新的点歌引导'] if old_rotates else ['旧欢迎甲、乙旧介绍'])
    sender.reset_mock()
    service.row('123456')['pending'] = [(0, '3', '丙'), (0, '4', '丁')]
    await service.deliver_welcome('123456')
    messages = [call.args[1] for call in sender.await_args_list]
    assert messages == (['新欢迎丙、丁新介绍'] if old_rotates else ['新欢迎丙、丁', '新介绍', '新的点歌引导'])
    service.store.close()
