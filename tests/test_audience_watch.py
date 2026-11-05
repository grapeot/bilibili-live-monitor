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


def test_timeout_stops_owned_tree_without_advancing(tmp_path, monkeypatch):
    from bilibili_live_monitor import session_runner
    settings, _ = setup(tmp_path)
    monkeypatch.setattr(watch, 'advance', lambda session, state: state.update(audience_session='owned-session'))
    assert watch.tick(settings)['status'] == 'running'
    monkeypatch.setattr(watch.time, 'time', lambda: 10**12)
    stopped = []
    monkeypatch.setattr(session_runner, 'stop_ai', lambda session, identifier, **kwargs: stopped.append((identifier, kwargs)) or True)
    monkeypatch.setattr(watch, 'advance', lambda *_: (_ for _ in ()).throw(AssertionError('超时后不能推进')))
    assert watch.tick(settings)['status'] == 'attention'
    assert stopped == [('owned-session', {'children': True})]
