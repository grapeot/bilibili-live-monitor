import json
from datetime import datetime
from pathlib import Path

from bilibili_live_monitor import audience_watch as watch


def setup(tmp_path):
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    targets = []
    originals = {}
    for index, (room, stage) in enumerate((('10001', 'completed'), ('10002', 'completed'),
                                           ('10001', 'recording'))):
        directory = sessions / str(index)
        directory.mkdir()
        start = f'2026-01-0{index + 1}T00:00:00+00:00'
        config = {'workspace': str(directory), 'database': str(tmp_path / 'monitor.sqlite3'),
                  'room_id': room, 'label': '示例', 'start_utc': start,
                  'live_start': f'2026-01-0{index + 1} 08:00:00',
                  'deadline': datetime.fromisoformat(start).timestamp() + 3600,
                  'workflow_key': 'example_' + str(index), 'ai_enabled': False}
        (directory / 'config.json').write_text(json.dumps(config))
        workflow = directory / 'workflow.json'
        workflow.write_text(json.dumps({'stage': stage, 'notification_sent': True}))
        originals[workflow] = workflow.read_bytes()
        if index < 2:
            target = tmp_path / f'crm_{room}.json'
            target.write_text(json.dumps({'room_id': room, 'sessions_root': str(sessions)}))
            targets.append({'config': str(target), 'not_before': '2026-01-01T00:00:00+00:00'})
    return {'workspace': str(tmp_path / 'watch'), 'targets': targets}, originals


def test_serial_updates_use_separate_state_and_leave_original_workflows(tmp_path, monkeypatch):
    settings, originals = setup(tmp_path)
    calls = []

    def advance(session, state):
        calls.append(session['room_id'])
        assert session['audience_state_path'] != str(Path(session['workspace']) / 'workflow.json')
        if not state.get('started'):
            state['started'] = True
            return False
        state['audience_crm'] = {'plan_id': 'example', 'people': 2, 'published': False}
        return True

    monkeypatch.setattr(watch, 'advance', advance)
    assert watch.tick(settings)['status'] == 'running'
    assert watch.tick(settings)['status'] == 'completed'
    assert watch.tick(settings)['status'] == 'running'
    assert watch.tick(settings)['status'] == 'completed'
    assert watch.tick(settings) == {'status': 'idle'}
    assert calls == ['10001', '10001', '10002', '10002']
    assert all(path.read_bytes() == value for path, value in originals.items())


def test_failed_stop_never_advances_again_and_other_room_continues_after_cleanup(tmp_path, monkeypatch):
    from bilibili_live_monitor import session_runner
    settings, _ = setup(tmp_path)
    calls, stops = [], []
    outcomes = iter((False, True))

    def advance(session, state):
        calls.append(session['room_id'])
        if session['room_id'] == '10001':
            state['audience_session'] = 'owned-session'
            raise ValueError('示例失败')
        state['audience_crm'] = {'plan_id': 'example', 'people': 2, 'published': False}
        return True

    def stop(session, identifier, *, children):
        stops.append((identifier, children))
        return next(outcomes)

    monkeypatch.setattr(watch, 'advance', advance)
    monkeypatch.setattr(session_runner, 'stop_ai', stop)
    assert watch.tick(settings)['status'] == 'stopping'
    assert watch.tick(settings)['status'] == 'attention'
    assert watch.tick(settings)['status'] == 'completed'
    assert watch.tick(settings)['status'] == 'idle'
    assert calls == ['10001', '10002']
    assert stops == [('owned-session', True), ('owned-session', True)]


def test_timeout_persistently_pauses_without_stopping_owned_tree(tmp_path, monkeypatch):
    from bilibili_live_monitor import session_runner
    settings, _ = setup(tmp_path)
    monkeypatch.setattr(watch, 'advance', lambda session, state: state.update(audience_session='owned-session'))
    assert watch.tick(settings)['status'] == 'running'
    monkeypatch.setattr(watch.time, 'time', lambda: 10**12)
    stopped = []
    monkeypatch.setattr(session_runner, 'stop_ai', lambda session, identifier, **kwargs: stopped.append((identifier, kwargs)) or True)
    monkeypatch.setattr(watch, 'advance', lambda *_: (_ for _ in ()).throw(AssertionError('超时后不能推进')))
    assert watch.tick(settings)['status'] == 'paused'
    assert watch.tick(settings)['status'] == 'paused'
    assert stopped == []
    active = json.loads((Path(settings['workspace']) / 'active.json').read_text())
    state = json.loads(Path(active['state_file']).read_text())
    assert state['audience_session'] == 'owned-session'
    assert state['automation_paused']['reason'] == 'finish_timeout'


def test_stop_retries_preserve_original_error_and_expose_cleanup_failure(tmp_path, monkeypatch):
    from bilibili_live_monitor import session_runner
    settings, _ = setup(tmp_path)
    monkeypatch.setattr(watch, 'advance', lambda *a: (_ for _ in ()).throw(ValueError('original')))
    monkeypatch.setattr(session_runner, 'stop_ai', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('cleanup')))
    first = watch.tick(settings)
    assert first['status'] == 'stopping'
    for _ in range(3):
        assert watch.tick(settings)['error'] == first['error'] == 'ValueError: original'
    active = json.loads((Path(settings['workspace']) / 'active.json').read_text())
    assert active['stop_error'] == 'RuntimeError: cleanup'


def test_recovery_hold_prevents_selection_or_advance(tmp_path, monkeypatch):
    settings, _ = setup(tmp_path)
    root = Path(settings['workspace'])
    root.mkdir()
    (root / 'recovery_hold.json').write_text(json.dumps({'recovery_id': 'owned-recovery'}))
    monkeypatch.setattr(watch, 'advance', lambda *a: (_ for _ in ()).throw(AssertionError('must not advance')))
    assert watch.tick(settings) == {'status': 'recovery_hold', 'recovery_id': 'owned-recovery'}
    assert not (root / 'active.json').exists()


def test_independent_budget_survives_config_changes_and_boundary_pause(tmp_path, monkeypatch):
    from bilibili_live_monitor.session_budget import BudgetPaused, check_budget
    settings, originals = setup(tmp_path)
    def advance(session, state):
        check_budget(session)  # 与ensure_ai/dispatch相同的磁盘读取路径。
        state['audience_session'] = 'owned-session'
        raise BudgetPaused('publication boundary')
    monkeypatch.setattr(watch, 'advance', advance)
    assert watch.tick(settings)['status'] == 'paused'
    active = json.loads((Path(settings['workspace']) / 'active.json').read_text())
    deadline = json.loads(Path(active['state_file']).read_text())['finish_deadline']
    assert deadline == active['started_at'] + 21600
    settings['timeout_seconds'] = 99999999999
    assert watch.tick(settings)['status'] == 'paused'
    assert json.loads(Path(active['state_file']).read_text())['finish_deadline'] == deadline
    assert all(path.read_bytes() == value for path, value in originals.items())


def test_actual_worker_reconcile_error_reaches_watch_then_cleanup_succeeds(tmp_path, monkeypatch):
    from bilibili_live_monitor import cursor_finalize, session_runner
    settings, _ = setup(tmp_path)
    monkeypatch.setattr(watch, 'advance', lambda *a: (_ for _ in ()).throw(ValueError('original')))
    monkeypatch.setattr(session_runner, 'stop_ai', lambda *a, **kw: True)
    # stop_workers must not swallow its underlying reconciliation error.
    monkeypatch.setattr(cursor_finalize, 'stop_workers', lambda *a, **kw: (_ for _ in ()).throw(OSError('launcher read failed')))
    assert watch.tick(settings)['status'] == 'stopping'
    active = json.loads((Path(settings['workspace']) / 'active.json').read_text())
    assert active['last_error'] == 'ValueError: original'
    assert active['stop_error'] == 'OSError: launcher read failed'
    monkeypatch.setattr(cursor_finalize, 'stop_workers', lambda *a, **kw: True)
    assert watch.tick(settings)['status'] == 'attention'
    assert json.loads((Path(settings['workspace']) / 'active.json').read_text()) is None
