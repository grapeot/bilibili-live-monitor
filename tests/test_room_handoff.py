import json
import time
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from bilibili_live_monitor.room_handoff import advance


@pytest.fixture
def setup(tmp_path):
    identity = {'room_id': '123456', 'workflow_key': 'synthetic', 'start_utc': '2030-01-01T00:00:00Z'}
    (tmp_path / 'config.json').write_text(json.dumps(identity))
    (tmp_path / 'workflow.json').write_text(json.dumps({'stage': 'sealing', 'cutoff': 100}))
    config = {**identity, 'session_directory': str(tmp_path), 'source_api': 'http://source',
              'destination_api': 'http://destination'}
    policy = {'enabled': True, 'mode': 'fixed', 'target_rooms': 1, 'fixed_rooms': ['https://live.bilibili.com/123456']}
    source = {'policy': policy, 'capacity': 2, 'revision': 1, 'launching': 0, 'pending_pages': 0,
              'candidates': [{'room_id': '123456', 'live_status': 0, 'offline_checks': 2,
                              'url':'https://live.bilibili.com/123456','blocked':False,
                              'status_checked_utc': datetime.now(timezone.utc).isoformat()}],
              'sources': [{'room_id': '123456', 'status': 'stopped'}]}
    destination = {'policy': deepcopy(policy), 'capacity': 1, 'revision': 1, 'launching': 0, 'pending_pages': 0,
                   'candidates':[{'url':'https://live.bilibili.com/123456','blocked':True,
                                  'last_error':'operator_stopped'}], 'sources':[]}
    room = {'room_id': '123456', 'enabled': False}
    snapshots = {'http://source/api/platform': source, 'http://destination/api/platform': destination,
                 'http://source/api/automation': {'rooms': [room]},
                 'http://destination/api/automation': {'rooms': [room],
                     'account': {'mode': 'anonymous', 'logged_in': False}}}
    writes = []
    def api(url, data=None, method=None):
        if data is None:
            return deepcopy(snapshots[url])
        writes.append((url, deepcopy(data)))
        if url.endswith('/decisions'):
            platform=snapshots[url.removesuffix('/decisions')]
            assert data['revision']==platform['revision']
            for candidate in platform['candidates']:
                if candidate['url'] in data['stop']:
                    candidate.update(blocked=True, last_error='ai_stopped')
            platform['revision']+=1
        elif url.endswith('/unpark'):
            assert data['revision']==destination['revision']
            destination['candidates'][0].update(blocked=False, last_error=None)
        else:
            pytest.fail('handoff must not write full policy')
        return {'accepted': True}
    return config, snapshots, writes, api


def test_handoff_only_changes_policies_after_boundary_and_is_idempotent(setup, tmp_path):
    config, snapshots, writes, api = setup
    original = (tmp_path / 'config.json').read_bytes(), (tmp_path / 'workflow.json').read_bytes()
    assert advance(config, {}, api=api)['phase'] == 'ready'
    assert not writes
    state = {}
    advance(config, state, api=api, execute=True)
    assert state['phase'] == 'completed'
    assert writes[0][0] == 'http://source/api/platform/decisions'
    assert writes[-1][0] == 'http://destination/api/platform/candidates/123456/unpark'
    assert snapshots['http://source/api/platform']['candidates'][0]['blocked'] is True
    assert original == ((tmp_path / 'config.json').read_bytes(), (tmp_path / 'workflow.json').read_bytes())
    count = len(writes)
    advance(config, state, api=api, execute=True)
    assert len(writes) == count


@pytest.mark.parametrize('change', ['live', 'stale', 'capturing', 'unsealed', 'single_offline', 'launching', 'pending'])
def test_current_capture_or_unconfirmed_boundary_never_mutates(setup, tmp_path, change):
    config, snapshots, writes, api = setup
    source = snapshots['http://source/api/platform']
    if change == 'live':
        source['candidates'][0]['live_status'] = 1
    if change == 'stale':
        source['candidates'][0]['status_checked_utc'] = datetime.fromtimestamp(time.time()-100, timezone.utc).isoformat()
    if change == 'capturing':
        source['sources'][0]['status'] = 'capturing'
    if change == 'unsealed':
        (tmp_path / 'workflow.json').write_text('{"stage":"live"}')
    if change == 'single_offline':
        source['candidates'][0]['offline_checks'] = 1
    if change == 'launching':
        source['launching'] = 1
    if change == 'pending':
        source['pending_pages'] = 1
    assert advance(config, {}, api=api, execute=True)['phase'] == 'waiting_boundary'
    assert not writes


def test_failed_destination_activation_recovers_after_source_release(setup):
    config, snapshots, writes, api = setup
    state = {}
    def failed(url, data=None, method=None):
        if url.endswith('/unpark') and data is not None:
            raise OSError('synthetic response failure')
        return api(url, data, method)
    with pytest.raises(OSError):
        advance(config, state, api=failed, execute=True)
    assert state['phase'] == 'activating_destination'
    assert snapshots['http://source/api/platform']['candidates'][0]['blocked']
    assert advance(config, state, api=api, execute=True)['phase'] == 'completed'


@pytest.mark.parametrize('change', ['logged_in', 'welcome', 'capacity', 'identity'])
def test_invalid_destination_or_identity_fails_before_any_write(setup, tmp_path, change):
    config, snapshots, writes, api = setup
    if change == 'logged_in':
        snapshots['http://destination/api/automation']['account']['logged_in'] = True
    if change == 'welcome':
        snapshots['http://destination/api/automation']['rooms'][0]['enabled'] = True
    if change == 'capacity':
        snapshots['http://destination/api/platform']['capacity'] = 0
    if change == 'identity':
        config['workflow_key'] = 'different-session'
    with pytest.raises(ValueError):
        advance(config, {}, api=api, execute=True)
    assert not writes


def test_all_canonical_aliases_are_retired_and_pending_launch_is_drained(setup):
    config, snapshots, writes, api = setup
    source = snapshots['http://source/api/platform']
    alias = {**source['candidates'][0], 'url':'https://live.bilibili.com/42', 'alias_duplicate':True}
    source['candidates'].append(alias)
    source['policy']['fixed_rooms'].append(alias['url'])
    source['policy']['target_rooms'] = 2
    def raced(url, data=None, method=None):
        result = api(url,data,method)
        if url.endswith('/decisions') and data is not None:
            source['launching']=1
        return result
    state={}
    assert advance(config,state,api=raced,execute=True)['phase']=='retiring_source'
    assert len(writes)==1 and set(writes[0][1]['stop'])==set(source['policy']['fixed_rooms'])
    source['launching']=0
    assert advance(config,state,api=api,execute=True)['phase']=='completed'


def test_unknown_alias_fails_closed(setup):
    config, snapshots, writes, api = setup
    source=snapshots['http://source/api/platform']
    source['policy']['fixed_rooms'].append('https://live.bilibili.com/42')
    source['policy']['target_rooms']=2
    source['candidates'].append({'url':'https://live.bilibili.com/42','blocked':False})
    with pytest.raises(ValueError,match='规范身份'):
        advance(config,{},api=api,execute=True)
    assert not writes


def test_real_revision_checked_retirement_preserves_other_session_asr():
    from bilibili_live_monitor.platform import Decision, Platform
    platform=Platform.__new__(Platform)
    platform.revision=3
    platform.candidates={'https://live.bilibili.com/123456':{'blocked':False,'selected':False}}
    platform.session_asr={'654321':{'start':'synthetic','enabled':True}}
    platform.discovery_requested=False
    platform.save=lambda:None
    platform.event=lambda *args,**kwargs:None
    decision=Decision(revision=3,stop=['https://live.bilibili.com/123456'],reason='authorized handoff')
    platform.apply_decision(decision)
    assert platform.session_asr['654321']['enabled'] is True
    assert platform.candidates['https://live.bilibili.com/123456']['blocked']
    with pytest.raises(ValueError,match='过期'):
        platform.apply_decision(decision)


def test_failed_stop_before_apply_rechecks_live_boundary_on_retry(setup):
    config, snapshots, writes, api = setup
    state={}
    def failed(url,data=None,method=None):
        if url.endswith('/decisions') and data is not None:
            raise OSError('request not applied')
        return api(url,data,method)
    with pytest.raises(OSError):
        advance(config,state,api=failed,execute=True)
    source=snapshots['http://source/api/platform']
    source['candidates'][0].update(live_status=1,offline_checks=0)
    source['sources'][0]['status']='capturing'
    assert advance(config,state,api=api,execute=True)['phase']=='waiting_boundary'
    assert not writes


@pytest.mark.parametrize('change',['refusal','revision','reason'])
def test_real_conditional_unpark_never_clears_new_refusal(change):
    from bilibili_live_monitor.platform import Platform
    platform=Platform.__new__(Platform)
    platform.revision=3
    platform.status_refused=None
    platform.candidates={'https://live.bilibili.com/123456':{'url':'https://live.bilibili.com/123456',
                         'blocked':True,'last_error':'operator_stopped'}}
    platform.save=lambda:None
    platform.event=lambda *args,**kwargs:None
    if change=='refusal':
        platform.status_refused='HTTP 412'
    if change=='revision':
        platform.revision=4
    if change=='reason':
        platform.candidates['https://live.bilibili.com/123456']['last_error']='HTTP 403'
    with pytest.raises(ValueError):
        platform.unpark_candidate('123456',3)
    assert platform.candidates['https://live.bilibili.com/123456']['blocked']
    if change=='refusal':
        assert platform.status_refused=='HTTP 412'


def test_unpark_http_checks_version_and_preserves_global_refusal(tmp_path):
    from fastapi.testclient import TestClient

    from bilibili_live_monitor.app import create_app
    from bilibili_live_monitor.config import Settings
    app=create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        client.post('/api/platform/candidates',json={'candidates':[{'url':'123456'}]})
        revision=client.post('/api/platform/candidates/123456/stop').json()['revision']
        app.state.platform.status_refused='HTTP 412'
        assert client.post('/api/platform/candidates/123456/unpark',json={'revision':revision}).status_code==409
        assert app.state.platform.status_refused=='HTTP 412'
        app.state.platform.status_refused=None
        assert client.post('/api/platform/candidates/123456/unpark',json={'revision':revision-1}).status_code==409
        assert client.post('/api/platform/candidates/123456/unpark',json={'revision':revision}).status_code==202
