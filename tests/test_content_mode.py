import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from bilibili_live_monitor import session_runner as runner
from bilibili_live_monitor.content_mode import apply_content_mode
from bilibili_live_monitor.live_share import iso, make_snapshot, render_snapshot
from bilibili_live_monitor.platform import Platform
from bilibili_live_monitor.room_config import validate_rooms


def no_ai(*args, **kwargs):
    pytest.fail('talk mode must not invoke a content agent')


def test_talk_created_and_live_never_preflight_create_or_dispatch(tmp_path, monkeypatch):
    config = {'workspace': str(tmp_path), 'content_mode': 'talk', 'remote': 'host:/share/',
              'ai_enabled': False, 'talk_finalize': True}
    state = {'stage': 'created', 'remote_prepared': True}
    monkeypatch.setattr(runner, 'ai_control', no_ai)
    monkeypatch.setattr(runner.watch, 'ai_once', no_ai)
    monkeypatch.setattr(runner, 'source_at_cutoff', no_ai)
    runner.advance(config, state, 1)
    assert state['stage'] == 'live' and not config.get('ai_session')
    monkeypatch.setattr(runner.watch, 'cycle', lambda *a: {'phase': 'live'})
    monkeypatch.setattr(runner, 'verify_local', lambda *a: {})
    monkeypatch.setattr(runner, 'verify_remote', lambda *a: None)
    runner.advance(config, state, 301)
    assert state['ready'] and 'ai_slot' not in state


def test_talk_stops_legacy_agent_before_any_work(tmp_path, monkeypatch):
    config = {'workspace': str(tmp_path), 'content_mode': 'talk', 'ai_enabled': True, 'ai_session': 'old'}
    state = {'stage': 'created','remote_prepared':True}
    monkeypatch.setattr(runner, 'stop_ai', Mock(side_effect=[False,True]))
    monkeypatch.setattr(runner, 'ai_control', no_ai)
    runner.advance(config, state, 1)
    assert state['stage'] == 'created'
    runner.advance(config, state, 2)
    assert config['ai_session'] is None and config['ai_enabled'] is False


def test_talk_seals_and_transcribes_before_finalizing_without_agent(tmp_path, monkeypatch):
    from bilibili_live_monitor import session_offline
    config = {'workspace':str(tmp_path),'content_mode':'talk','finalize_parallel':True,
              'api':'https://example.org','room_id':'1','live_start':'synthetic','seal_seconds':0}
    state = {'stage':'stopping','cutoff':90}
    monkeypatch.setattr(runner, 'ai_control', no_ai)
    offline = Mock(return_value=str(tmp_path/'offline_segments.json'))
    monkeypatch.setattr(runner, 'run_offline', offline)
    monkeypatch.setattr(runner, 'recording_files', lambda *a: {})
    monkeypatch.setattr(runner.watch, 'request', lambda *a: {'candidates':[], 'sources':[]})
    monkeypatch.setattr(session_offline, 'verify_offline', lambda *a: {})
    for name in ('export_snapshot','verify_local','verify_remote'):
        monkeypatch.setattr(runner,name,lambda *a: None)
    monkeypatch.setattr(runner.watch,'publish',lambda *a: None)
    runner.advance(config,state,100)
    assert state['stage']=='sealing'
    runner.advance(config,state,101)
    runner.advance(config,state,102)
    assert state['stage']=='offline_asr'
    runner.advance(config,state,103)
    assert state['stage']=='finalizing'
    offline.assert_called_once()
    runner.advance(config,state,104)
    assert state['stage']=='completed'


def test_talk_cannot_publish_without_offline_completion(tmp_path, monkeypatch):
    config={'workspace':str(tmp_path),'content_mode':'talk'}
    monkeypatch.setattr(runner.watch,'publish',no_ai)
    with pytest.raises(ValueError, match='离线转写尚未接入'):
        runner.advance(config, {'stage':'finalizing','cutoff':90}, 100)


def test_new_talk_waits_for_committed_refinement_and_stops_writers_before_publish(tmp_path, monkeypatch):
    from bilibili_live_monitor import session_offline, session_timeline, talk_finalize
    config = {'workspace': str(tmp_path), 'content_mode': 'talk', 'talk_finalize': True, 'final_timeline': True}
    state = {'stage': 'offline_asr', 'cutoff': 90}
    monkeypatch.setattr(runner, 'run_offline', lambda *a: str(tmp_path / 'offline_segments.json'))
    monkeypatch.setattr(runner, 'ensure_ai', lambda c, p: 'offline-only' if p == 'talk_finalize' else no_ai())
    monkeypatch.setattr(runner.watch, 'ai_once', no_ai)
    monkeypatch.setattr(talk_finalize, 'prepare', lambda *a: {'id': 'synthetic'})
    sends = []
    def submit(config, session, on_handoff):
        on_handoff()
        sends.append(session)
    monkeypatch.setattr(talk_finalize, 'submit', submit)
    monkeypatch.setattr(talk_finalize, 'progress', lambda *a: {})
    committed = False
    monkeypatch.setattr(talk_finalize, 'complete', lambda *a: committed)
    runner.advance(config, state, 100)
    assert state['stage'] == 'offline_review'
    runner.advance(config, state, 101)
    assert state['final_session'] == 'offline-only'
    runner.advance(config, state, 102)
    runner.advance(config, state, 103)
    assert state['stage'] == 'offline_review' and sends == ['offline-only']
    committed = True
    runner.advance(config, state, 104)
    assert state['stage'] == 'finalizing'
    monkeypatch.setattr(session_offline, 'verify_offline', lambda *a: {})
    monkeypatch.setattr(runner, 'stop_ai', Mock(side_effect=[False, True, True]))
    published = []
    archived = []
    monkeypatch.setattr(session_timeline, 'commit', lambda *a: archived.append(True) or {})
    monkeypatch.setattr(session_timeline, 'verify', lambda *a: None)
    monkeypatch.setattr(runner.watch, 'publish', lambda *a: published.append(True))
    for name in ('export_snapshot', 'verify_local', 'verify_remote'):
        monkeypatch.setattr(runner, name, lambda *a: None)
    runner.advance(config, state, 105)
    assert state['stage'] == 'finalizing' and not published
    committed = False
    with pytest.raises(ValueError, match='尚未完整入库'):
        runner.advance(config, state, 106)
    assert not published
    committed = True
    runner.advance(config, state, 107)
    assert state['stage'] == 'completed' and len(published) == len(archived) == 1


def test_talk_renderer_hides_song_features_without_labeling_asr_as_reviewed():
    snapshot=make_snapshot({'items':[],'segments':[],'raw':[]},start=iso(100),now=iso(200),
        label='测试',phase='live',health={})
    original=deepcopy(snapshot)
    apply_content_mode(snapshot,{'content_mode':'talk'})
    html=render_snapshot(snapshot)
    assert snapshot['activity']==[] and snapshot['followups']==[]
    assert 'id="followups"' not in html and 'id="activity-summary"' not in html
    assert 'id="session-summary"' in html and 'setInterval' in html
    assert snapshot['series']==original['series'] and snapshot['counts']==original['counts']


def test_capture_override_is_per_room_and_keeps_asr():
    from bilibili_live_monitor.platform import Policy
    platform=object.__new__(Platform)
    platform.policy=Policy(record_mode='video',asr_enabled=True)
    platform.room_capture_overrides={'123456':{'record_mode':'none'}}
    platform.session_asr={}
    platform.session_workflow_rooms=set()
    assert platform.capture_options({'room_id':'123456'})['record_mode']=='none'
    assert platform.capture_options({'room_id':'123456'})['asr_enabled']
    assert platform.capture_options({'room_id':'654321'})['record_mode']=='video'


@pytest.mark.parametrize('fields',[{'content_mode':'invalid'},{'record_mode':'invalid'}])
def test_modes_validate_before_apply(fields):
    with pytest.raises(ValueError):
        validate_rooms({'123456':{'label':'测试','intro':'欢迎',**fields}})


def test_live_build_applies_talk_mode_before_render(tmp_path, monkeypatch):
    from bilibili_live_monitor import live_share_watch as watch
    config={'workspace':str(tmp_path),'room_id':'123456','content_mode':'talk',
            'start_utc':iso(100),'label':'测试'}
    monkeypatch.setattr(watch,'read_session_source',lambda c: {'items':[],'segments':[],'raw':[]})
    monkeypatch.setattr(watch,'verified_titles',lambda p: ())
    api={'rooms':[{'room_id':'123456','recorded_seconds':0,'recording_bytes':0,
        'recording_status':'disabled','asr_enabled':True}], 'asr_ready':True,'queue_depth':0}
    watch.build(config,{'last_live_at':200},{},api,200,upload=False)
    snapshot=json.loads((tmp_path/'public/snapshot.json').read_text())
    assert snapshot['content_mode']=='talk'
    assert 'id="followups"' not in (tmp_path/'public/index.html').read_text()


def test_live_room_mode_propagates_to_existing_session(tmp_path):
    from test_session_workflow import automation, settings

    from bilibili_live_monitor.session_workflow import SessionWorkflows
    service,candidate=automation(tmp_path)
    manager=SessionWorkflows(service,settings(tmp_path))
    record=manager.prepare('123456',candidate)
    path=manager.initialize(record)
    original=json.loads(path.read_text())
    original.update(ai_session='old',ai_enabled=True)
    path.write_text(json.dumps(original))
    service.rooms['123456']['content_mode']='talk'
    manager.initialize(record)
    current=json.loads(path.read_text())
    assert current['content_mode']=='talk' and not current['ai_enabled']
    assert current['url']==original['url'] and current['ai_session']=='old'
    assert 'health_enabled' not in current


def test_new_talk_gets_refinement_and_timeline_but_old_completed_config_is_untouched(tmp_path):
    from test_session_workflow import automation, settings

    from bilibili_live_monitor.session_workflow import SessionWorkflows
    service, candidate = automation(tmp_path)
    service.rooms['123456']['content_mode'] = 'talk'
    manager = SessionWorkflows(service, settings(tmp_path))
    record = manager.prepare('123456', candidate)
    path = manager.initialize(record)
    current = json.loads(path.read_text())
    assert current['talk_finalize'] is True and current['final_timeline'] is True and current['ai_enabled'] is False
    current.pop('talk_finalize')
    current.pop('final_timeline')
    path.write_text(json.dumps(current))
    before = path.read_bytes()
    record['stage'] = 'completed'
    manager.initialize(record)
    assert path.read_bytes() == before
