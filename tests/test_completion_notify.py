import hashlib
import json
import sqlite3

import pytest

from bilibili_live_monitor import completion_notify as module
from bilibili_live_monitor.live_share import epoch


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / 'sessions'
    folder = root / 'one'
    (folder / 'public').mkdir(parents=True)
    start, end = '2030-01-01T00:00:00+00:00', '2030-01-01T01:00:00+00:00'
    url = 'https://example.org/share/live-one/'
    record = {'key': 'one', 'room_id': '1', 'start': start, 'workspace': str(folder),
              'url': url, 'stage': 'completed'}
    local = {'room_id': '1', 'start_utc': start, 'workspace': str(folder), 'workflow_key': 'one',
             'content_mode': 'talk', 'ai_enabled': False, 'url': url}
    workflow = {'stage': 'completed', 'ready': True, 'completed_at': end, 'cutoff': epoch(end), 'last_error': None}
    snapshot = {'phase': 'completed', 'content_mode': 'talk', 'session_start': start, 'updated_at': end}
    for name, value in [('config.json', local), ('workflow.json', workflow), ('public/snapshot.json', snapshot)]:
        (folder / name).write_text(json.dumps(value))
    (folder / 'public/index.html').write_text('<html>final</html>')
    (folder/'offline').mkdir()
    (folder/'offline/manifest.json').write_text(json.dumps({'cutoff':epoch(end),'rows':[{'path':'synthetic'}]}))
    (folder/'offline_segments.json').write_text('[{"seq":-1}]')
    (folder/'offline/status.json').write_text('{"counts":{"done":1}}')
    marker={'room_id':'1','start_utc':start,'cutoff':epoch(end),'files':1,'segments':1,
            'manifest_sha256':hashlib.sha256((folder/'offline/manifest.json').read_bytes()).hexdigest(),
            'segments_sha256':hashlib.sha256((folder/'offline_segments.json').read_bytes()).hexdigest()}
    (folder/'offline/completed.json').write_text(json.dumps(marker))
    local['offline_segments']=str(folder/'offline_segments.json')
    (folder/'config.json').write_text(json.dumps(local))
    parent = {'event': 'offline', 'room_id': '1', 'live_start': start, 'status': 'accepted',
              'message_id': '<offline@example.org>', 'receipt': 'parent', 'subject': 'offline'}
    database = tmp_path / 'monitor.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE control(key TEXT PRIMARY KEY,data TEXT)')
        db.executemany('INSERT INTO control VALUES (?,?)', [
            ('session_workflows', json.dumps({'one': record})),
            ('automation', json.dumps({'notifications': {'offline': parent}}))])
    config = {'room_id': '1', 'database': str(database), 'sessions_root': str(root),
              'public_origin': 'https://example.org', 'completion_notifications': {
                  'enabled': True, 'not_before': start, 'display_name': '合成主播',
                  'ledger': str(tmp_path / 'ledger.json'), 'command': ['synthetic']}}
    calls = []
    def adapter(command, payload=None, extra=()):
        calls.append((payload, extra))
        return {'valid': True} if extra else {'id': 'sent-one'}
    monkeypatch.setattr(module, 'call_adapter', adapter)
    monkeypatch.setattr(module, 'verify_snapshot', lambda _: {'valid': True})
    monkeypatch.setattr(module, 'verify_remote', lambda _: None)
    return config, record, folder, calls


def test_talk_with_offline_asr_without_ai_artifacts_sends_once_and_threads(setup):
    config, record, folder, calls = setup
    assert module.notify(config, dry_run=True)[0]['status'] == 'ready'
    assert (folder / 'offline/completed.json').exists()
    assert not (folder / 'finalize').exists()
    assert not module.Path(config['completion_notifications']['ledger']).exists()
    assert module.notify(config)[0]['status'] == 'accepted'
    assert module.notify(config)[0]['skipped']
    sent = [p for p, extra in calls if not extra]
    assert len(sent) == 1 and sent[0]['in_reply_to'] == '<offline@example.org>'
    assert '离线转写' in sent[0]['html'] and '不进行歌曲AI终审' in sent[0]['html']


@pytest.mark.parametrize('name,field,value', [
    ('config.json', 'content_mode', 'music'), ('config.json', 'ai_enabled', True),
    ('config.json', 'room_id', '2'), ('workflow.json', 'stage', 'live'),
    ('workflow.json', 'last_error', 'error'), ('public/snapshot.json', 'phase', 'live'),
    ('public/snapshot.json', 'updated_at', '2030-01-01T00:30:00+00:00'),
    ('public/snapshot.json', 'content_mode', 'music')])
def test_invalid_final_state_cannot_send(setup, name, field, value):
    config, _, folder, calls = setup
    path = folder / name
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    assert module.notify(config)[0]['status'] == 'waiting'
    assert calls == []


def test_remote_mismatch_blocks_mail(setup, monkeypatch):
    config, _, _, calls = setup
    def mismatch(_):
        raise ValueError('remote mismatch')
    monkeypatch.setattr(module, 'verify_remote', mismatch)
    assert module.notify(config)[0]['status'] == 'waiting'
    assert calls == []


def test_offline_incomplete_or_modified_cannot_notify(setup):
    config, _, folder, calls = setup
    (folder/'offline_segments.json').write_text('[]')
    assert module.notify(config)[0]['status'] == 'waiting'
    assert calls == []


def test_submission_uncertainty_never_retries(setup, monkeypatch):
    config, _, _, calls = setup
    def fail(command, payload=None, extra=()):
        calls.append((payload, extra))
        if extra:
            return {'valid': True}
        raise TimeoutError('response lost')
    monkeypatch.setattr(module, 'call_adapter', fail)
    assert module.notify(config)[0]['status'] == 'unknown'
    assert module.notify(config)[0]['skipped']
    assert len([p for p, extra in calls if not extra]) == 1


def test_room_and_cutoff_scope(setup):
    config, _, _, calls = setup
    config['room_id'] = 'other'
    assert module.notify(config) == []
    config['room_id'] = '1'
    config['completion_notifications']['not_before'] = '2030-01-02T00:00:00+00:00'
    assert module.notify(config) == []
    assert calls == []


def test_new_talk_requires_database_refinement_before_notification(setup, monkeypatch):
    from bilibili_live_monitor import talk_finalize
    config, _, folder, calls = setup
    path = folder / 'config.json'
    local = json.loads(path.read_text())
    local['talk_finalize'] = True
    path.write_text(json.dumps(local))
    monkeypatch.setattr(talk_finalize, 'complete', lambda c: False)
    assert module.notify(config)[0]['status'] == 'waiting' and not calls
    monkeypatch.setattr(talk_finalize, 'complete', lambda c: True)
    assert module.notify(config)[0]['status'] == 'accepted'
    sent = [p for p, extra in calls if not extra]
    assert len(sent) == 1 and 'AI转写精修' in sent[0]['html']


def test_final_timeline_commit_is_required_before_new_notification(setup, monkeypatch):
    from bilibili_live_monitor import session_timeline
    config, _, folder, calls = setup
    path = folder / 'config.json'
    local = json.loads(path.read_text())
    local['final_timeline'] = True
    path.write_text(json.dumps(local))
    def fail(*args):
        raise ValueError('最终时间线尚未入库')
    monkeypatch.setattr(session_timeline, 'verify', fail)
    assert module.notify(config)[0]['status'] == 'waiting' and not calls
    monkeypatch.setattr(session_timeline, 'verify', lambda *a: {})
    assert module.notify(config)[0]['status'] == 'accepted'
