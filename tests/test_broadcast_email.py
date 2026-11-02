import io
import json

import pytest

from bilibili_live_monitor import broadcast_email


@pytest.mark.parametrize('available', [True, False, 'timeout'])
def test_send_reads_actual_provider_message_id_and_never_resends(monkeypatch, capsys, available):
    monkeypatch.setattr('sys.argv', ['test', '--env-file', '/nonexistent/notify.env'])
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'id': 'synthetic', 'subject': 'Test', 'html': '<p>test</p>'})))
    for key, value in {'BILI_NOTIFY_FROM': 'notice@example.org', 'BILI_NOTIFY_TO': 'owner@example.org',
                       'RESEND_API_KEY': 'synthetic'}.items():
        monkeypatch.setenv(key, value)
    budgets = []
    monkeypatch.setattr(broadcast_email.time, 'sleep', budgets.append)
    requests = []
    def urlopen(request, **kwargs):
        requests.append(request)
        budgets.append(kwargs['timeout'])
        assert request.get_header('User-agent') == 'bilibili-live-monitor/0.1'
        if request.get_method() == 'GET' and available == 'timeout':
            raise TimeoutError('metadata timeout')
        value = {'id': 'provider-id'} if request.get_method() == 'POST' else {
            'message_id': '<actual-provider@example.org>' if available is True else None}
        return io.StringIO(json.dumps(value))
    monkeypatch.setattr(broadcast_email.urllib.request, 'urlopen', urlopen)
    assert broadcast_email.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert sum(r.get_method() == 'POST' for r in requests) == 1
    assert result['id'] == 'provider-id'
    assert result['message_id'] == ('<actual-provider@example.org>' if available is True else None)
    assert bool(result['thread_error']) == (available is not True)
    assert sum(budgets) <= 65  # 调用方90秒内仍可保存已接受回执，预留进程启动余量。
