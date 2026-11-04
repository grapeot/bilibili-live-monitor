import json
import sqlite3

import pytest

from bilibili_live_monitor import session_health as health
from bilibili_live_monitor.live_share import iso


def config(tmp_path):
    db = tmp_path / 'source.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE segments(room_id TEXT, data TEXT)')
        conn.execute('INSERT INTO segments VALUES (?,?)', ('123456', json.dumps(
            {'end_utc': iso(1000), 'text': 'synthetic', 'is_final': True})))
    (tmp_path / 'workflow.json').write_text('{"stage":"live"}')
    return {'workspace': str(tmp_path), 'database': str(db), 'room_id': '123456',
            'start_utc': iso(0), 'live_start': '2030-01-01 08:00:00',
            'api': 'http://synthetic', 'launcher': 'http://launcher', 'workflow_key': 'test'}


def fake_api(cfg, calls, *, status='error', live=True, mode='audio', asr=True):
    def request(url, data=None):
        calls.append((url, data))
        if url.endswith('/api/state'):
            return {'rooms': [{'room_id': '123456', 'status': status, 'recording_bytes': 100,
                              'record_mode': mode, 'asr_enabled': asr}], 'asr_ready': asr}
        if url.endswith('/api/platform'):
            return {'candidates': [{'room_id': '123456', 'live_status': int(live),
                     'live_start': cfg['live_start'], 'status_checked_utc': iso(999)}]}
        return {}
    return request


def test_recovery_bounded_and_offline_never_resumed(tmp_path, monkeypatch):
    cfg, calls = config(tmp_path), []
    monkeypatch.setattr(health, 'request', fake_api(cfg, calls))
    assert health.inspect(cfg, {}, 1000)['recovery'] == 'room_resume_requested'
    calls.clear()
    assert 'recovery' not in health.inspect(cfg, {'recoveries': [998, 999]}, 1000)
    assert not any(data is not None for _, data in calls)
    monkeypatch.setattr(health, 'request', fake_api(cfg, calls, live=False))
    assert 'recovery' not in health.inspect(cfg, {}, 1000)
    assert not any(data is not None for _, data in calls)


@pytest.mark.parametrize('mode,stalled', [('none', False), ('audio', True), ('video', True)])
def test_stall_only_for_recorded_rooms(tmp_path, monkeypatch, mode, stalled):
    cfg, calls = config(tmp_path), []
    monkeypatch.setattr(health, 'request', fake_api(cfg, calls, status='capturing', mode=mode, asr=False))
    assert 'capture_stalled' not in health.inspect(cfg, {}, 1000)['issues']
    issues = health.inspect(cfg, {'checked_epoch': 700, 'recording_bytes': 100}, 1000)['issues']
    assert ('capture_stalled' in issues) is stalled
    assert 'asr_unhealthy' not in issues


def test_error_windows_do_not_mask_asr_staleness(tmp_path, monkeypatch):
    cfg, calls = config(tmp_path), []
    with sqlite3.connect(cfg['database']) as db:
        db.execute('DELETE FROM segments')
        db.execute('INSERT INTO segments VALUES (?,?)', ('123456', json.dumps(
            {'end_utc': iso(999), 'text': '', 'status': 'error', 'error': 'input lost'})))
    monkeypatch.setattr(health, 'request', fake_api(cfg, calls, status='capturing'))
    assert 'asr_stale' in health.inspect(cfg, {}, 1000)['issues']
    with sqlite3.connect(cfg['database']) as db:
        db.execute('INSERT INTO segments VALUES (?,?)', ('123456', json.dumps(
            {'end_utc': iso(999), 'text': '', 'status': 'empty', 'error': None})))
    assert 'asr_stale' not in health.inspect(cfg, {}, 1000)['issues']


def test_failed_resume_does_not_stop_room_and_counts_attempt(tmp_path, monkeypatch):
    cfg, calls = config(tmp_path), []
    base = fake_api(cfg, calls)
    def request(url, data=None):
        if url.endswith('/resume'):
            raise TimeoutError('synthetic')
        return base(url, data)
    monkeypatch.setattr(health, 'request', request)
    result = health.inspect(cfg, {}, 1000)
    assert 'recovery_error' in result and result['recoveries'] == [1000]
    assert json.loads((tmp_path / 'health.json').read_text())['recoveries'] == [1000]
    assert not any(url.endswith('/stop') for url, _ in calls)


def test_schedule_failure_does_not_block_publisher(tmp_path, monkeypatch):
    from bilibili_live_monitor import session_runner as runner
    cfg = config(tmp_path)
    monkeypatch.setattr(runner, 'load_config', lambda *a: cfg)
    monkeypatch.setattr(health, 'ensure_scheduled', lambda *a, **kw: (_ for _ in ()).throw(TimeoutError('synthetic')))
    called = []
    def advance(config, state, now):
        called.append(True)
        state['stage'] = 'completed'
    monkeypatch.setattr(runner, 'advance', advance)
    assert runner.run(tmp_path / 'config.json') == 0
    assert called == [True]
    assert 'TimeoutError' in json.loads((tmp_path / 'workflow.json').read_text())['health_schedule_error']


def test_completed_stops_chain(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    (tmp_path / 'workflow.json').write_text('{"stage":"completed"}')
    monkeypatch.setattr(health, 'request', lambda *a: pytest.fail('terminal network call'))
    assert health.run(cfg, reschedule=True)['stopped']


def test_refused_capture_is_not_resumed(tmp_path, monkeypatch):
    cfg, calls = config(tmp_path), []
    base = fake_api(cfg, calls)
    def request(url, data=None):
        result = base(url, data)
        if url.endswith('/api/state'):
            result['rooms'][0]['error'] = 'recording_refused: HTTP 403'
        return result
    monkeypatch.setattr(health, 'request', request)
    result = health.inspect(cfg, {}, 1000)
    assert 'platform_refused' in result['issues']
    assert not any(data is not None for _, data in calls)


def test_schedule_timeout_keeps_current_health_evidence(tmp_path, monkeypatch):
    cfg, calls = config(tmp_path), []
    monkeypatch.setattr(health, 'request', fake_api(cfg, calls, status='capturing'))
    monkeypatch.setattr(health, 'schedule', lambda *a: (_ for _ in ()).throw(TimeoutError('synthetic')))
    result = health.run(cfg, reschedule=True)
    assert 'TimeoutError' in result['schedule_error']
    assert json.loads((tmp_path / 'health.json').read_text()) == result
    assert json.loads((tmp_path / 'health_history.jsonl').read_text()) == result


def test_existing_schedule_reused_and_disabled_flag_ignored(monkeypatch):
    monkeypatch.setattr(health, 'request', lambda *a: [{'id': 'existing', 'label': 'session_health_test'}])
    assert health.schedule({'launcher': 'http://synthetic', 'workflow_key': 'test',
                            'health_enabled': False}) == 'existing'


@pytest.mark.parametrize('failure', [True, False])
def test_talk_runner_always_schedules_without_blocking_publisher(tmp_path, monkeypatch, failure):
    from bilibili_live_monitor import session_runner as runner
    cfg = {**config(tmp_path), 'content_mode': 'talk', 'ai_enabled': False, 'health_enabled': False}
    monkeypatch.setattr(runner, 'load_config', lambda *a: cfg)
    scheduled = []
    def schedule(*args, **kwargs):
        scheduled.append(True)
        if failure:
            raise TimeoutError('synthetic')
    monkeypatch.setattr(health, 'ensure_scheduled', schedule)
    def advance(config, state, now):
        state['stage'] = 'completed'
    monkeypatch.setattr(runner, 'advance', advance)
    assert runner.run(tmp_path / 'config.json') == 0
    assert scheduled == [True]
    assert ('health_schedule_error' in json.loads((tmp_path / 'workflow.json').read_text())) is failure
