import json
import sqlite3

from bilibili_live_monitor import session_health as health
from bilibili_live_monitor.live_share import iso


def config(tmp_path):
    db = tmp_path / 'source.sqlite3'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE segments(room_id TEXT, data TEXT)')
        c.execute('INSERT INTO segments VALUES (?,?)', ('123456', json.dumps(
            {'end_utc': iso(1000), 'text': 'synthetic', 'is_final': True})))
    (tmp_path / 'workflow.json').write_text('{"stage":"live"}')
    (tmp_path / 'annotations.json').write_text('{}')
    return {'workspace': str(tmp_path), 'database': str(db), 'room_id': '123456',
            'start_utc': iso(0), 'live_start': '2030-01-01 08:00:00',
            'api': 'http://synthetic', 'launcher': 'http://launcher', 'workflow_key': 'test'}


def fake_api(cfg, calls, *, room_status='error', candidate_live=True):
    def request(url, data=None):
        calls.append((url, data))
        if url.endswith('/api/state'):
            return {'rooms': [{'room_id': '123456', 'status': room_status, 'recording_bytes': 100}],
                    'asr_ready': True, 'queue_depth': 0}
        if url.endswith('/api/platform'):
            return {'candidates': [{'room_id': '123456', 'live_status': int(candidate_live),
                    'live_start': cfg['live_start'], 'status_checked_utc': iso(999)}]}
        return {}
    return request


def test_recovery_only_current_live_and_maximum_twice(tmp_path, monkeypatch):
    cfg, calls = config(tmp_path), []
    monkeypatch.setattr(health, 'request', fake_api(cfg, calls))
    first = health.inspect(cfg, {}, 1000)
    assert first['recovery'] == 'room_resume_requested'
    assert [url.rsplit('/', 1)[-1] for url, data in calls if data is not None] == ['resume']
    calls.clear()
    result = health.inspect(cfg, {'recoveries': [998, 999]}, 1000)
    assert 'recovery' not in result
    assert not any(data is not None for _, data in calls)


def test_offline_candidate_never_restarted(tmp_path, monkeypatch):
    cfg, calls = config(tmp_path), []
    monkeypatch.setattr(health, 'request', fake_api(cfg, calls, candidate_live=False))
    assert 'recovery' not in health.inspect(cfg, {}, 1000)
    assert not any(data is not None for _, data in calls)


def test_stalled_bytes_detected_without_treating_first_sample_as_stall(tmp_path, monkeypatch):
    cfg, calls = config(tmp_path), []
    monkeypatch.setattr(health, 'request', fake_api(cfg, calls, room_status='capturing'))
    assert 'capture_stalled' not in health.inspect(cfg, {}, 1000)['issues']
    previous = {'checked_epoch': 700, 'recording_bytes': 100}
    assert 'capture_stalled' in health.inspect(cfg, previous, 1000)['issues']


def test_terminal_workflow_stops_chain(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    (tmp_path / 'workflow.json').write_text('{"stage":"completed"}')
    monkeypatch.setattr(health, 'request', lambda *a: (_ for _ in ()).throw(AssertionError('network')))
    assert health.run(cfg, reschedule=True)['stopped']


def test_schedule_reuses_existing_pending_job(monkeypatch):
    monkeypatch.setattr(health, 'request', lambda *a: [{'id': 'existing', 'label': 'session_health_test'}])
    assert health.schedule({'launcher': 'http://synthetic', 'workflow_key': 'test'}) == 'existing'


def test_error_windows_do_not_hide_stale_asr_but_empty_success_is_valid(tmp_path, monkeypatch):
    cfg, calls = config(tmp_path), []
    with sqlite3.connect(cfg['database']) as db:
        db.execute('DELETE FROM segments')
        for row in ({'end_utc': iso(100), 'text': 'old', 'is_final': True, 'status': 'done'},
                    {'end_utc': iso(999), 'text': '', 'status': 'error', 'error': 'input lost'}):
            db.execute('INSERT INTO segments VALUES (?,?)', ('123456', json.dumps(row)))
    monkeypatch.setattr(health, 'request', fake_api(cfg, calls, room_status='capturing'))
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
