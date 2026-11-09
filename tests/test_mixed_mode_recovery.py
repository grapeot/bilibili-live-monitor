import hashlib
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from bilibili_live_monitor import session_runner as runner
from bilibili_live_monitor.activity_summary import accounting_errors
from bilibili_live_monitor.content_mode import apply_content_mode
from bilibili_live_monitor.live_share import iso, make_snapshot, render_snapshot


@pytest.mark.parametrize('phase', ['live', 'completed'])
def test_talk_keeps_gifts_accounted_without_claiming_reviewed_content(phase):
    source = {'raw': [], 'segments': [], 'items': [
        ('gift', iso(150), {'gift': {'event_type': 'SEND_GIFT', 'coin_type': 'gold',
         'total_coin': 100, 'num': 1, 'event_at_utc': iso(150), 'time_source': 'event'}})]}
    snapshot = make_snapshot(source, start=iso(100), now=iso(200), label='测试', phase=phase, health={})
    total = deepcopy(snapshot['session_summary'])
    apply_content_mode(snapshot, {'content_mode': 'talk'})
    assert accounting_errors(snapshot) == []
    assert snapshot['session_summary'] == total
    assert snapshot['activity_summary'][0]['kind'] == 'unclassified'
    assert 'id="activity-summary"' not in render_snapshot(snapshot)


@pytest.mark.parametrize('ended', [False, True])
def test_runner_verifies_before_upload_and_never_uploads_invalid_snapshot(tmp_path, monkeypatch, ended):
    config = {'workspace': str(tmp_path), 'content_mode': 'talk', 'remote': 'host:/share/'}
    state = {'stage': 'live', 'ready': True}
    cycle = Mock(return_value={'phase': 'completed' if ended else 'live', 'cutoff': 200})
    monkeypatch.setattr(runner.watch, 'cycle', cycle)
    publish = Mock()
    monkeypatch.setattr(runner.watch, 'publish', publish)
    def invalid(_):
        raise RuntimeError('invalid')
    monkeypatch.setattr(runner, 'verify_local', invalid)
    if ended:
        runner.advance(config, state, 300)
        assert state['stage'] == 'stopping'
        publish.assert_not_called()
        return
    with pytest.raises(RuntimeError, match='invalid'):
        runner.advance(config, state, 300)
    assert cycle.call_args.args[0]['_defer_publish'] is True
    publish.assert_not_called()
    monkeypatch.setattr(runner, 'verify_local', lambda _: {})
    (tmp_path / 'public').mkdir()
    (tmp_path / 'public/snapshot.json').write_text('{}')
    runner.advance(config, state, 301)
    publish.assert_called_once()
    assert state['stage'] == ('stopping' if ended else 'live')


def test_music_completion_requires_explicit_opt_in_and_final_proof(setup, monkeypatch):  # noqa: F811
    from bilibili_live_monitor import completion_notify, session_finalize
    config, record, folder, calls = setup
    for name in ('config.json', 'public/snapshot.json'):
        path = folder / name
        value = json.loads(path.read_text())
        value['content_mode'] = 'music'
        if name == 'config.json':
            value.update(ai_enabled=True, finalize_parallel=True)
        path.write_text(json.dumps(value))
    assert completion_notify.notify(config)[0]['status'] == 'waiting'
    config['completion_notifications']['content_modes'] = ['talk', 'music']
    monkeypatch.setattr(session_finalize, 'complete', lambda _: False)
    assert completion_notify.notify(config)[0]['status'] == 'waiting'
    monkeypatch.setattr(session_finalize, 'complete', lambda _: True)
    (folder / 'annotations.json').write_text('{}')
    (folder / 'verified_songs.json').write_text('[]')
    path = folder / 'workflow.json'
    workflow = json.loads(path.read_text())
    workflow['final_artifacts'] = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                                  for name in ('public/index.html', 'public/snapshot.json', 'annotations.json', 'verified_songs.json')}
    path.write_text(json.dumps(workflow))
    (folder / 'annotations.json').write_text('{"changed":true}')
    assert completion_notify.notify(config)[0]['status'] == 'waiting'
    (folder / 'annotations.json').write_text('{}')
    assert completion_notify.notify(config)[0]['status'] == 'accepted'
    assert completion_notify.notify(config)[0]['skipped']
    sent = [p for p, extra in calls if not extra]
    assert len(sent) == 1 and '杂谈模式' not in sent[0]['html']


def test_terminal_error_still_enters_sealing_without_uploading(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.watch, 'cycle', lambda *a: {
        'phase': 'completed', 'cutoff': 200, 'last_error': 'temporary API failure'})
    publish = Mock()
    monkeypatch.setattr(runner.watch, 'publish', publish)
    state = {'stage': 'live'}
    runner.advance({'workspace': str(tmp_path), 'content_mode': 'talk'}, state, 300)
    assert state['stage'] == 'stopping' and state['cutoff'] == 200
    publish.assert_not_called()


def test_once_respects_existing_publisher_owner(tmp_path, monkeypatch):
    import fcntl
    import sys

    from bilibili_live_monitor import live_share_watch as watch
    monkeypatch.setattr(sys, 'argv', ['watch', 'once', '--config', str(tmp_path / 'config.json')])
    monkeypatch.setattr(watch, 'load_config', lambda _: {'workspace': str(tmp_path)})
    cycle = Mock()
    monkeypatch.setattr(watch, 'cycle', cycle)
    with (tmp_path / 'publisher.lock').open('a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            watch.main()
    cycle.assert_not_called()


def test_attention_does_not_render_live_pill():
    snapshot = make_snapshot({'raw': [], 'segments': [], 'items': []},
                             start=iso(100), now=iso(200), label='测试', phase='live', health={})
    snapshot['publication_status'] = 'attention'
    html = render_snapshot(snapshot)
    assert '页面更新异常' in html and '<span class="live-pill">' not in html


def test_terminal_fallback_cannot_publish_unverified_public_snapshot(tmp_path, monkeypatch):
    from bilibili_live_monitor import live_share_watch as watch
    public = tmp_path / 'public'
    public.mkdir()
    (public / 'snapshot.json').write_text('{"unverified":true}')
    monkeypatch.setattr(watch, 'request', lambda *a: (_ for _ in ()).throw(OSError('offline')))
    publish = Mock()
    monkeypatch.setattr(watch, 'publish', publish)
    config = {'workspace': str(tmp_path), 'deadline': 200, 'start_utc': iso(100), 'api': 'unused', 'remote': 'host:/share/'}
    state = watch.cycle(config, tmp_path / 'state.json', 'stop')
    assert state['phase'] == 'completed'
    publish.assert_not_called()


from test_completion_notify import setup  # noqa: E402,F401,F811
