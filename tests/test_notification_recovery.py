import asyncio
import io
import json
import sys
from unittest.mock import AsyncMock

import pytest
from test_session_workflow import automation, settings

from bilibili_live_monitor import broadcast_email
from bilibili_live_monitor.session_workflow import SessionWorkflows


@pytest.mark.asyncio
async def test_missing_metadata_recovered_without_resending_opening(tmp_path, monkeypatch):
    service, candidate = automation(tmp_path)
    service.notification_command = ['python', '-m', 'bilibili_live_monitor.broadcast_email']
    service.observe_state('123456')
    manager = SessionWorkflows(service, settings(tmp_path))
    record = manager.prepare('123456', candidate)
    opening = next(iter(service.state['notifications'].values()))
    opening.update(status='accepted', receipt='receipt-1', subject='opening')
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (json.dumps({'id': 'receipt-1', 'message_id': '<actual@example.org>'}).encode(), b'')
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    assert not manager.notify_url(record, {'ready': True})
    assert not manager.notify_url(record, {'ready': True})
    await service.tasks['metadata:' + opening['id']]
    launch.assert_awaited_once()
    assert launch.call_args.args[-2:] == ('--lookup-receipt', 'receipt-1')
    assert manager.notify_url(record, {'ready': True})
    assert service.state['notifications'][record['key'] + '-dashboard']['in_reply_to'] == '<actual@example.org>'
    assert opening['status'] == 'accepted'


def test_adapter_cached_receipt_prevents_duplicate_post(tmp_path, monkeypatch, capsys):
    env = tmp_path / '.env'
    env.write_text('')
    monkeypatch.setenv('BILI_NOTIFY_FROM', 'notice@example.org')
    monkeypatch.setenv('BILI_NOTIFY_TO', 'owner@example.org')
    monkeypatch.setenv('RESEND_API_KEY', 'synthetic')
    monkeypatch.setattr(sys, 'argv', ['adapter', '--env-file', str(env)])
    payload = {'id': 'unique', 'subject': 'test', 'html': '<p>test</p>'}
    calls = []
    def request(req, **kwargs):
        calls.append(req.get_method())
        return io.StringIO(json.dumps({'id': 'receipt-1'}))
    monkeypatch.setattr(broadcast_email.urllib.request, 'urlopen', request)
    monkeypatch.setattr(broadcast_email, 'lookup_message_id', lambda _: '<actual@example.org>')
    for _ in range(2):
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
        assert broadcast_email.main() == 0
        assert json.loads(capsys.readouterr().out)['id'] == 'receipt-1'
    assert calls == ['POST']
