import json

import httpx
import pytest
from fastapi.testclient import TestClient

from bilibili_live_monitor.dashboard_gateway import create_gateway, room_snapshot, validate_instances


def test_waiting_rooms_are_visible_without_capture_or_database_mutation(tmp_path):
    state = {'rooms': [{'room_id': '1', 'status': 'stopped', 'items': [], 'segments': [], 'error': 'old'}]}
    platform = {'policy': {'enabled': True, 'fixed_rooms': ['https://live.bilibili.com/1', 'https://live.bilibili.com/2'],
                           'asr_enabled': True, 'record_mode': 'audio'},
                'candidates': [{'url': 'https://live.bilibili.com/1', 'room_id': '1', 'live_status': 0},
                               {'url': 'https://live.bilibili.com/2', 'room_id': '2', 'live_status': 0, 'title': '待播'}]}
    auto = {'account': {'mode': 'anonymous'}, 'session_workflows': []}
    result = room_snapshot(state, platform, auto, {'id': 'guest', 'label': '匿名'})
    assert len(result['rooms']) == 2
    assert all(r['status'] == 'waiting' and r['instance_label'] == '匿名' for r in result['rooms'])
    assert state['rooms'][0]['status'] == 'stopped'
    assert result['rooms'][1]['asr_enabled'] is True


@pytest.mark.parametrize('url', ['https://example.org', 'http://127.0.0.1:1/api', 'http://user@127.0.0.1:1'])
def test_only_explicit_local_instance_endpoints(url):
    with pytest.raises(ValueError):
        validate_instances([{'id': 'one', 'label': 'one', 'url': url}])


def test_proxy_isolation_and_anonymous_controls(tmp_path):
    calls = []

    def upstream(request):
        calls.append((request.url.port, request.url.path, request.method))
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok":true}'), headers={'Content-Type': 'application/json'})

    config = {'instances': [{'id': 'account', 'label': '账号', 'url': 'http://127.0.0.1:8765'},
                            {'id': 'guest', 'label': '匿名', 'mode': 'anonymous', 'url': 'http://127.0.0.1:8766',
                             'session_root': str(tmp_path)}]}
    with TestClient(create_gateway(config, transport=httpx.MockTransport(upstream))) as client:
        response = client.post('/instances/guest/api/rooms/123/stop')
        assert response.status_code == 200
        assert calls[-1] == (8766, '/api/rooms/123/stop', 'POST')
        assert client.put('/instances/guest/api/automation/rooms/123', json={'enabled': True}).status_code == 403
        assert client.post('/instances/guest/api/automation/account/login').status_code == 403
        assert client.post('/instances/account/api/rooms/123/stop').status_code == 200
        assert calls[-1][0] == 8765
        assert client.get('/instances/unknown/api/export').status_code == 404
        assert client.get('/instances/guest/sessions/' + 'a' * 24 + '/config.json').status_code == 404


def test_final_page_only_serves_public_assets(tmp_path):
    key = 'a' * 24
    public = tmp_path / key / 'public'
    public.mkdir(parents=True)
    (public / 'index.html').write_text('final page')
    (public / 'snapshot.json').write_text(json.dumps({'phase': 'completed'}))
    (tmp_path / key / 'config.json').write_text('private')
    config = {'instances': [{'id': 'guest', 'label': '匿名', 'url': 'http://127.0.0.1:8766',
                             'session_root': str(tmp_path)}]}
    with TestClient(create_gateway(config)) as client:
        assert client.get(f'/instances/guest/sessions/{key}/').text == 'final page'
        assert client.get(f'/instances/guest/sessions/{key}/snapshot.json').json()['phase'] == 'completed'
        assert client.get(f'/instances/guest/sessions/{key}/config.json').status_code == 404


def test_browser_automation_omits_private_metadata_but_state_keeps_result_link(tmp_path):
    key = 'b' * 24
    public = tmp_path / key / 'public'
    public.mkdir(parents=True)
    (public / 'index.html').write_text('result')
    auto = {'account': {'mode': 'anonymous', 'profiles': [{'id': 'guest', 'label': '匿名', 'directory': 'SECRET'}]},
            'rooms': [{'room_id': '1', 'label': '测试', 'error': 'SECRET', 'private': 'SECRET'}],
            'session_workflows': [{'room_id': '1', 'key': key, 'stage': 'completed', 'workspace': 'SECRET', 'remote': 'SECRET'}],
            'workflow_errors': {'private': 'SECRET'}, 'likes': [{'id': 'like', 'private': 'SECRET'}],
            'notifications': [{'id': 'notification', 'private': 'SECRET'}]}

    def upstream(request):
        data = auto if request.url.path == '/api/automation' else (
            {'rooms': [{'room_id': '1', 'status': 'stopped'}]} if request.url.path == '/api/state' else {})
        return httpx.Response(200, json=data)

    config = {'instances': [{'id': 'guest', 'label': '匿名', 'url': 'http://127.0.0.1:8766', 'session_root': str(tmp_path)}]}
    with TestClient(create_gateway(config, transport=httpx.MockTransport(upstream))) as client:
        response = client.get('/instances/guest/api/automation')
        assert response.status_code == 200 and 'SECRET' not in response.text
        assert response.json()['account']['profiles'] == [{'id': 'guest', 'label': '匿名'}]
        assert client.get('/instances/guest/api/state').json()['rooms'][0]['result_url'] == f'sessions/{key}/'


@pytest.mark.parametrize(('method', 'path'), [('PUT', 'automation/rooms/1'), ('POST', 'automation/account')])
def test_automation_mutation_summaries_do_not_expose_workflow_paths(method, path):
    def upstream(request):
        assert request.method == method and request.url.path == '/api/' + path
        assert json.loads(request.content) == {'value': True}
        return httpx.Response(200, json={'account': {}, 'rooms': [],
            'session_workflows': [{'workspace': 'SECRET', 'remote': 'SECRET'}]})

    config = {'instances': [{'id': 'account', 'label': '账号', 'url': 'http://127.0.0.1:8765'}]}
    with TestClient(create_gateway(config, transport=httpx.MockTransport(upstream))) as client:
        response = client.request(method, '/instances/account/api/' + path, json={'value': True})
        assert response.status_code == 200 and 'SECRET' not in response.text
        assert set(response.json()) == {'account', 'rooms', 'likes', 'notifications'}


@pytest.mark.parametrize('content', ['{broken', 'null', None])
def test_optional_workflow_metadata_does_not_break_healthy_state(tmp_path, content):
    key = 'c' * 24
    directory = tmp_path / key
    directory.mkdir()
    if content is not None:
        (directory / 'workflow.json').write_text(content)
    auto = {'session_workflows': [{'room_id': '1', 'key': key, 'stage': 'offline_review'}]}

    def upstream(request):
        return httpx.Response(200, json=auto if request.url.path == '/api/automation' else (
            {'rooms': [{'room_id': '1', 'status': 'recording'}, {'room_id': '2', 'status': 'waiting'}]}
            if request.url.path == '/api/state' else {}))

    config = {'instances': [{'id': 'guest', 'label': '匿名', 'url': 'http://127.0.0.1:8766', 'session_root': str(tmp_path)}]}
    with TestClient(create_gateway(config, transport=httpx.MockTransport(upstream))) as client:
        response = client.get('/instances/guest/api/state')
        assert response.status_code == 200
        assert [r['status'] for r in response.json()['rooms']] == ['recording', 'waiting']
        assert response.json()['rooms'][0]['workflow_stage'] == 'offline_review'


def test_metrics_are_routed_to_the_correct_instance():
    def upstream(request):
        assert request.url.path == '/assets/resource_metrics.json'
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps({'port': request.url.port}).encode()),
                              headers={'Content-Type': 'application/json'})

    config = {'instances': [{'id': 'account', 'label': '账号', 'url': 'http://127.0.0.1:8765'},
                            {'id': 'guest', 'label': '匿名', 'url': 'http://127.0.0.1:8766'}]}
    with TestClient(create_gateway(config, transport=httpx.MockTransport(upstream))) as client:
        assert client.get('/instances/account/assets/resource_metrics.json').json() == {'port': 8765}
        assert client.get('/instances/guest/assets/resource_metrics.json').json() == {'port': 8766}
        assert client.get('/instances/guest/assets/private.json').status_code == 404
