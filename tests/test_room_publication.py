import json

import pytest
from test_session_workflow import automation, settings

from bilibili_live_monitor.room_config import validate_rooms
from bilibili_live_monitor.session_workflow import SessionWorkflows


def test_one_anonymous_room_can_publish_and_notify_without_enabling_other_rooms(tmp_path):
    service, candidate = automation(tmp_path)
    service.notification_command = []
    service.rooms['123456'].update(local_only=False, notification_command=['synthetic-mail'])
    service.rooms['654321'] = {'label': '匿名', 'intro': '不发送'}
    cfg = settings(tmp_path)
    cfg['local_only'] = True
    manager = SessionWorkflows(service, cfg)
    record = manager.prepare('123456', candidate)
    result = json.loads(manager.initialize(record).read_text())
    assert result['local_only'] is False
    assert service.notification_command_for('123456') == ['synthetic-mail']
    assert service.notification_command_for('654321') == []
    assert service.notification_command == []
    service.store.close()


@pytest.mark.parametrize('fields', [{'local_only': 1}, {'notification_command': 'shell command'},
                                  {'notification_command': [None]}, {'notification_command': ['']}])
def test_invalid_per_room_publication_options_rejected(fields):
    with pytest.raises(ValueError):
        validate_rooms({'123456': {'label': '合成', 'intro': '合成', **fields}})
