import hashlib
import json
import struct
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bilibili_live_monitor import session_runner as runner
from bilibili_live_monitor.automation import Automation
from bilibili_live_monitor.broadcast_email import prepare
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.live_share import (
    atomic_json,
    epoch,
    fingerprint,
    iso,
    make_snapshot,
    render_snapshot,
)
from bilibili_live_monitor.live_share_control import load_config
from bilibili_live_monitor.platform import Candidate, Platform, Policy
from bilibili_live_monitor.session_ai import BRIDGE
from bilibili_live_monitor.session_offline import recording_files, run_offline
from bilibili_live_monitor.session_workflow import SessionWorkflows
from bilibili_live_monitor.store import Store

START = '2030-01-01T00:00:00+00:00'


def automation(tmp_path):
    store = Store(tmp_path / 'monitor.sqlite3')
    candidate = {'url': 'https://live.bilibili.com/123456', 'room_id': '123456',
                 'live_start': '2030-01-01 08:00:00', 'live_status': 1,
                 'status_checked_utc': iso(time.time())}
    platform = SimpleNamespace(store=store, monitor=SimpleNamespace(browser=None, settings=Settings(data_dir=tmp_path)),
        policy=SimpleNamespace(enabled=True, mode='fixed'), candidates={'123456': candidate},
        fixed_urls=lambda: {candidate['url']})
    return Automation(platform, SimpleNamespace(summary=lambda: {}), {
        'rooms': {'123456': {'label': '合成主播', 'intro': '欢迎'}}, 'notification_command': ['synthetic']}), candidate


def settings(tmp_path):
    return {'enabled': True, 'workspace_root': str(tmp_path / 'sessions'),
            'opencode_python': '/synthetic/.venv/bin/python', 'opencode_cwd': '/synthetic',
            'opencode_project': '/synthetic/project', 'model': 'provider/model',
            'publish': {'default': {'remote': 'test:/share/', 'url': 'https://example.org/share/'},
                        'rooms': {'123456': {'remote': 'test:/artist/', 'url': 'https://artist.example/s/'}}}}


def test_session_recovery_reuses_url_and_new_broadcast_gets_new_url(tmp_path):
    service, candidate = automation(tmp_path)
    manager = SessionWorkflows(service, settings(tmp_path))
    first = manager.prepare('123456', candidate)
    path = manager.initialize(first)
    config = load_config(path)
    assert config['url'].startswith('https://artist.example/s/live-')
    assert config['remote'].startswith('test:/artist/live-')
    assert config['start_utc'] == START
    recovered = SessionWorkflows(service, settings(tmp_path))
    assert recovered.prepare('123456', candidate)['url'] == first['url']
    assert recovered.initialize(first) == path
    candidate['live_start'] = '2030-01-02 08:00:00'
    assert recovered.prepare('123456', candidate)['url'] != first['url']


def test_initialization_recovers_after_base_files_before_workflow_config(tmp_path, monkeypatch):
    service, candidate = automation(tmp_path)
    manager = SessionWorkflows(service, settings(tmp_path))
    record = manager.prepare('123456', candidate)
    import bilibili_live_monitor.session_workflow as module
    original = module.atomic_json
    def fail_config(path, value):
        if path.name == 'config.json':
            raise OSError('synthetic crash')
        original(path, value)
    monkeypatch.setattr(module, 'atomic_json', fail_config)
    with pytest.raises(OSError):
        manager.initialize(record)
    monkeypatch.setattr(module, 'atomic_json', original)
    assert load_config(manager.initialize(record))['workflow_key'] == record['key']


def test_dashboard_reply_waits_for_opening_receipt_and_keeps_thread(tmp_path):
    service, candidate = automation(tmp_path)
    service.observe_state('123456')
    manager = SessionWorkflows(service, settings(tmp_path))
    record = manager.prepare('123456', candidate)
    opening = next(iter(service.state['notifications'].values()))
    manager.notify_url(record, {'ready': True})
    assert len(service.state['notifications']) == 1
    opening.update(status='accepted', message_id='<original@example.org>', subject='合成主播 · 检测到开播')
    manager.notify_url(record, {'ready': True})
    manager.notify_url(record, {'ready': True})
    reply = service.state['notifications'][record['key'] + '-dashboard']
    assert len(service.state['notifications']) == 2
    assert reply['in_reply_to'] == '<original@example.org>' and reply['url'] == record['url']
    assert reply['subject'] == 'Re: ' + opening['subject']


def test_email_adapter_pins_recipients_and_uses_rfc_thread_not_api_receipt():
    env = {'BILI_NOTIFY_FROM': 'Notifier <notice@example.org>', 'BILI_NOTIFY_TO': 'owner@example.org',
           'BILI_NOTIFY_CC': 'push@example.net'}
    payload = {'id': 'synthetic', 'subject': 'Re: hello', 'html': '<p>Link</p>',
               'in_reply_to': '<original@example.org>', 'to': 'untrusted@example.net'}
    body, message_id = prepare(payload, env)
    assert body['to'] == ['owner@example.org'] and body['cc'] == ['push@example.net']
    assert body['headers']['References'] == body['headers']['In-Reply-To'] == '<original@example.org>'
    assert body['headers']['Message-ID'] == message_id
    payload['in_reply_to'] = 'api-uuid-is-not-a-message-id'
    with pytest.raises(ValueError):
        prepare(payload, env)


@pytest.mark.asyncio
async def test_manager_reconciles_running_owner_without_launching_duplicate(tmp_path, monkeypatch):
    service, candidate = automation(tmp_path)
    manager = SessionWorkflows(service, settings(tmp_path))
    record = manager.prepare('123456', candidate)
    manager.initialize(record)
    calls = []
    def request(url, value=None):
        calls.append((url, value))
        return [{'label': 'session_monitor_' + record['key'], 'status': 'running'}]
    monkeypatch.setattr('bilibili_live_monitor.session_workflow.request', request)
    await manager.tick()
    assert len(calls) == 1 and calls[0][0].endswith('/processes')


def test_stopping_aborts_only_owned_ai_before_offline(tmp_path, monkeypatch):
    calls = []
    def control(config, action, **kw):
        calls.append((action, kw['session']))
        return {'type': 'busy'} if action == 'status' else True
    monkeypatch.setattr(runner, 'ai_control', control)
    config = {'workspace': str(tmp_path), 'ai_session': 'owned'}
    state = {'stage': 'stopping'}
    runner.advance(config, state, 10)
    assert state['stage'] == 'stopping'
    assert calls == [('status', 'owned'), ('abort', 'owned')]
    monkeypatch.setattr(runner, 'ai_control', lambda *a, **k: {'type': 'idle'})
    runner.advance(config, state, 20)
    assert state['stage'] == 'sealing'


def test_sealing_waits_for_file_stability_and_does_not_touch_live_recorder(tmp_path, monkeypatch):
    media = tmp_path / 'a-20300101T000005Z-b-00000.mkv'
    media.write_bytes(b'one')
    config = {'workspace': str(tmp_path), 'start_utc': START, 'recording_root': str(tmp_path),
              'room_id': '123456', 'live_start': '2030-01-01 08:00:00', 'api': 'http://unused', 'seal_seconds': 300}
    state = {'stage': 'sealing', 'cutoff': epoch(START) + 20}
    monkeypatch.setattr(runner.watch, 'request', lambda *a: {'candidates': [
        {'room_id': '123456', 'live_start': config['live_start'], 'live_status': 0}],
        'sources': [{'room_id': '123456', 'status': 'stopped'}]})
    runner.advance(config, state, 20)
    runner.advance(config, state, 100)
    assert state['stage'] == 'sealing'
    media.write_bytes(b'changed')
    runner.advance(config, state, 200)
    runner.advance(config, state, 499)
    assert state['stage'] == 'sealing'
    runner.advance(config, state, 501)
    assert state['stage'] == 'offline_asr'


def test_media_selection_excludes_previous_and_next_broadcast(tmp_path):
    for stamp in ('20291231T235000Z', '20300101T000005Z', '20300101T010000Z'):
        (tmp_path / f'a-{stamp}-b-00000.mkv').write_bytes(b'synthetic')
    groups = recording_files({'recording_root': str(tmp_path), 'start_utc': START}, epoch(START) + 100)
    assert len(groups) == 1
    assert next(iter(groups.values()))[0][1].name == 'a-20300101T000005Z-b-00000.mkv'


def test_final_publish_stays_same_url_and_only_happens_after_verification(tmp_path, monkeypatch):
    config = {'workspace': str(tmp_path), 'url': 'https://example.org/s/stable/'}
    state = {'stage': 'finalizing', 'final_session': 'owned-final'}
    events = []
    monkeypatch.setattr(runner, 'stop_ai', lambda *a: events.append('stop') or True)
    monkeypatch.setattr(runner, 'export_snapshot', lambda *a: events.append('export'))
    monkeypatch.setattr(runner, 'verify_local', lambda *a: events.append('verify'))
    monkeypatch.setattr(runner.watch, 'publish', lambda *a: events.append('publish'))
    monkeypatch.setattr(runner, 'verify_remote', lambda c: events.append(c['url']))
    runner.advance(config, state, 100)
    assert events == ['stop', 'export', 'verify', 'publish', 'https://example.org/s/stable/']
    assert state['stage'] == 'completed'


def test_final_page_removes_polling_but_handoff_page_keeps_it():
    data = make_snapshot({'items': [], 'segments': [], 'raw': []}, start=START,
                         now=iso(epoch(START) + 100), label='合成', phase='completed', health={})
    page = render_snapshot(data)
    assert 'setInterval' not in page and 'data-refresh="static"' in page
    data['phase'] = 'finalizing'
    page = render_snapshot(data)
    assert 'setInterval' in page and "clearInterval(timer)" in page


@pytest.mark.asyncio
async def test_new_broadcast_restores_asr_but_manual_degrade_survives_restart(tmp_path, monkeypatch):
    store = Store(tmp_path / 'monitor.sqlite3')
    monitor = SimpleNamespace(store=store, settings=Settings(data_dir=tmp_path), browser=None)
    platform = Platform(monitor)
    platform.import_candidates([Candidate(url='123456')])
    platform.configure(Policy(enabled=True, target_rooms=1, mode='fixed', fixed_rooms=['123456']))
    platform.session_workflow_rooms = {'123456'}
    row = {'room_id': '123456', 'live_status': 1, 'live_time': '2030-01-01 08:00:00', 'uid': '1'}
    monkeypatch.setattr('bilibili_live_monitor.platform.room_statuses', AsyncMock(return_value={'123456': row}))
    await platform.refresh_statuses()
    candidate = next(iter(platform.candidates.values()))
    assert platform.capture_options(candidate)['asr_enabled']
    platform.configure(Policy(enabled=True, target_rooms=1, mode='fixed', fixed_rooms=['123456'], asr_enabled=False))
    assert not platform.capture_options(candidate)['asr_enabled']
    recovered = Platform(monitor)
    recovered.session_workflow_rooms = {'123456'}
    await recovered.refresh_statuses()
    assert not recovered.capture_options(candidate)['asr_enabled']
    row['live_time'] = '2030-01-02 08:00:00'
    await recovered.refresh_statuses()
    assert recovered.capture_options(next(iter(recovered.candidates.values())))['asr_enabled']


def test_offline_uses_full_asr_pipeline_without_writing_source_or_duplicate_work(tmp_path, monkeypatch):
    from bilibili_live_monitor import offline_asr
    root = tmp_path / 'media'
    root.mkdir()
    media = root / 'a-20300101T000005Z-b-00000.mkv'
    media.write_bytes(b'synthetic-media')
    before = media.stat()
    def probe(root, path):
        stat = path.stat()
        return {'path': str(path), 'room_id': root.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'name': path.name,
                'sealed': 'sealed_readable', 'duration_sec': 2}
    calls = []
    class Worker:
        def __init__(self, *a):
            self.model = 'synthetic'
        def start(self):
            calls.append('start')
        def close(self):
            calls.append('close')
        def request(self, *a):
            return {'text': '合成转写'}
    monkeypatch.setattr(offline_asr, 'probe', probe)
    monkeypatch.setattr(offline_asr, 'Worker', Worker)
    monkeypatch.setattr(offline_asr, 'decode_pcm', lambda *a: struct.pack('<f', 0.1) * 32000)
    config = {'workspace': str(tmp_path), 'recording_root': str(root), 'start_utc': START,
              'recording_segment_seconds': 300, 'room_id': '123456', 'asr_python': 'synthetic', 'asr_model': 'synthetic'}
    state = {'cutoff': epoch(START) + 20}
    result = run_offline(config, state)
    segments = json.loads(__import__('pathlib').Path(result).read_text())
    assert len(segments) == 1 and segments[0]['seq'] < 0
    assert segments[0]['start_utc'] == iso(epoch(START) + 5)
    assert segments[0]['end_utc'] == iso(epoch(START) + 7)
    assert run_offline(config, state) == result and calls == ['start', 'close']
    assert media.read_bytes() == b'synthetic-media' and media.stat().st_mtime_ns == before.st_mtime_ns


def test_end_to_end_lifecycle_uses_offline_reviews_and_same_public_url(tmp_path, monkeypatch):
    service, candidate = automation(tmp_path)
    manager = SessionWorkflows(service, settings(tmp_path))
    record = manager.prepare('123456', candidate)
    path = manager.initialize(record)
    config = load_config(path)
    state = {'stage': 'created', 'ai_preflight': True}
    moment = epoch(START) + 60
    candidate['status_checked_utc'] = iso(moment)
    platform = {'candidates': [candidate], 'sources': [{'room_id': '123456', 'status': 'capturing'}]}
    api = {'rooms': [{'room_id': '123456', 'recorded_seconds': 50, 'recording_bytes': 123,
                      'recording_status': 'recording', 'asr_enabled': True}], 'asr_ready': True, 'queue_depth': 0}
    monkeypatch.setattr(runner.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(runner, 'ai_control', lambda c, op, **kw: {'id': kw.get('title', 'id'), 'type': 'idle'})
    monkeypatch.setattr(runner.watch, 'request', lambda u: platform if u.endswith('/platform') else api)
    monkeypatch.setattr(runner.watch.time, 'time', lambda: moment)
    published = []
    monkeypatch.setattr(runner.watch, 'publish', lambda c, d: published.append(c['url']))
    monkeypatch.setattr(runner, 'verify_remote', lambda *a: None)
    def review(c, s, source, now, **kw):
        if kw.get('final'):
            atomic_json(path.parent / 'annotations.json', {'reviews': [
                {'seq': row['seq'], 'fingerprint': fingerprint(row), 'label': 'speaking',
                 'music_source': 'none', 'investigation_version': 2} for row in source['segments']]})
    monkeypatch.setattr(runner.watch, 'ai_once', review)
    runner.advance(config, state, moment)
    config = load_config(path)
    runner.advance(config, state, moment)
    assert state['stage'] == 'live' and state['ready']
    candidate.update(live_status=0, offline_checks=2)
    platform['sources'][0]['status'] = 'stopped'
    runner.advance(config, state, moment)
    assert state['stage'] == 'stopping'
    runner.advance(config, state, moment)
    monkeypatch.setattr(runner, 'recording_files', lambda *a: {})
    runner.advance(config, state, moment)
    moment += 301
    runner.advance(config, state, moment)
    assert state['stage'] == 'offline_asr'
    def offline(c, s):
        destination = path.parent / 'offline_segments.json'
        atomic_json(destination, [{'seq': -1, 'start_utc': START, 'end_utc': iso(epoch(START) + 10),
                                   'text': '离线新增原文', 'is_final': True}])
        return str(destination)
    monkeypatch.setattr(runner, 'run_offline', offline)
    runner.advance(config, state, moment)
    config = load_config(path)
    runner.advance(config, state, moment)  # 创建finalize会话
    runner.advance(config, state, moment)  # 实际离线复核
    runner.advance(config, state, moment)  # 校验完整复核
    assert state['stage'] == 'finalizing'
    runner.advance(config, state, moment)
    assert state['stage'] == 'completed' and len(set(published)) == 1
    page = (path.parent / 'public/index.html').read_text()
    assert 'setInterval' not in page and '离线新增原文' not in page
    assert json.loads((path.parent / 'public/snapshot.json').read_text())['activity']


@pytest.mark.parametrize('operation', ['status', 'abort'])
def test_ai_bridge_uses_verified_session_directory_for_status_and_abort(monkeypatch, operation):
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs.get('params')))
        if url.endswith('/session/owned'):
            value = {'directory': '/correct/project'}
        elif url.endswith('/session/status'):
            value = {'owned': {'type': 'busy'}} if kwargs['params']['directory'] == '/correct/project' else {}
        else:
            value = True
        return SimpleNamespace(content=b'{}', json=lambda: value, raise_for_status=lambda: None)
    client = SimpleNamespace(base_url='https://synthetic.invalid', headers={},
                             _session=SimpleNamespace(request=request))
    monkeypatch.setitem(sys.modules, 'opencode_skill', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'opencode_skill.client', SimpleNamespace(OpenCodeClient=lambda: client))
    monkeypatch.setattr(sys, 'argv', ['test', json.dumps({'operation': operation, 'session': 'owned', 'project': '/correct/project'})])
    namespace = {}
    exec(BRIDGE, namespace)
    assert calls[-1][2] == {'directory': '/correct/project'}
    if operation == 'status':
        assert namespace['result']['type'] == 'busy'


def test_initial_config_temporary_file_crash_recovers_only_owned_files(tmp_path, monkeypatch):
    service, candidate = automation(tmp_path)
    manager = SessionWorkflows(service, settings(tmp_path))
    record = manager.prepare('123456', candidate)
    import bilibili_live_monitor.live_share_control as control
    original = control.atomic_json
    def crash(path, value):
        path.with_suffix(path.suffix + '.tmp').write_text('{incomplete')
        raise OSError('interrupted before rename')
    monkeypatch.setattr(control, 'atomic_json', crash)
    with pytest.raises(OSError):
        manager.initialize(record)
    monkeypatch.setattr(control, 'atomic_json', original)
    path = manager.initialize(record)
    assert load_config(path)['url'] == record['url']
    other = manager.prepare('123456', {**candidate, 'live_start': '2030-01-02 08:00:00'})
    from pathlib import Path
    unknown = Path(other['workspace'])
    unknown.mkdir()
    (unknown / 'unrelated.txt').write_text('do not touch')
    with pytest.raises(ValueError):
        manager.initialize(other)
    assert (unknown / 'unrelated.txt').read_text() == 'do not touch'


@pytest.mark.asyncio
async def test_invalid_live_start_does_not_block_existing_workflow(tmp_path, monkeypatch):
    service, candidate = automation(tmp_path)
    manager = SessionWorkflows(service, settings(tmp_path))
    record = manager.prepare('123456', candidate)
    path = manager.initialize(record)
    candidate['live_start'] = '0'
    atomic_json(path.parent / 'workflow.json', {'stage': 'live', 'ready': True})
    requests = []
    def request(url, value=None):
        requests.append(url)
        return [{'label': 'session_monitor_' + record['key'], 'status': 'running'}]
    monkeypatch.setattr('bilibili_live_monitor.session_workflow.request', request)
    await manager.tick()
    assert service.state['workflow_errors']['123456']
    assert len(requests) == 1  # 坏房间不阻断已有工作流维护。


@pytest.mark.asyncio
async def test_late_opening_receipt_still_notifies_completed_broadcast(tmp_path):
    service, candidate = automation(tmp_path)
    service.observe_state('123456')
    manager = SessionWorkflows(service, settings(tmp_path))
    record = manager.prepare('123456', candidate)
    path = manager.initialize(record)
    record['stage'] = 'completed'
    atomic_json(path.parent / 'workflow.json', {'stage': 'completed', 'ready': True})
    opening = next(iter(service.state['notifications'].values()))
    opening.update(status='accepted', message_id='<opening@example.org>', subject='Opening')
    await manager.tick()
    assert record['url_queued']
    assert service.state['notifications'][record['key'] + '-dashboard']['url'] == record['url']


def test_final_review_keeps_working_when_identified_title_is_not_registered(tmp_path, monkeypatch):
    service, candidate = automation(tmp_path)
    manager = SessionWorkflows(service, settings(tmp_path))
    path = manager.initialize(manager.prepare('123456', candidate))
    config = load_config(path)
    segment = {'seq': -1, 'start_utc': START, 'end_utc': iso(epoch(START) + 10), 'text': '合成歌词', 'is_final': True}
    config['offline_segments'] = str(path.parent / 'offline_segments.json')
    atomic_json(path.parent / 'offline_segments.json', [segment])
    atomic_json(path.parent / 'state.json', {'phase': 'completed', 'cutoff': epoch(START) + 60})
    atomic_json(path.parent / 'annotations.json', {'reviews': [{'seq': -1, 'fingerprint': fingerprint(segment),
        'label': 'singing', 'song': '合成待登记曲目', 'music_source': 'probable_live', 'investigation_version': 2}]})
    monkeypatch.setattr(runner, 'ai_control', lambda *a, **k: {'type': 'idle'})
    calls = []
    monkeypatch.setattr(runner.watch, 'ai_once', lambda *a, **k: calls.append(k))
    state = {'stage': 'offline_review', 'final_session': 'owned-final', 'cutoff': epoch(START) + 60}
    runner.advance(config, state, epoch(START) + 100)
    assert state['stage'] == 'offline_review' and state['unreviewed_windows'] == 0
    assert state['unpublished_songs'] == ['合成待登记曲目'] and calls == [{'final': True}]


def test_probe_failure_retries_and_never_certifies_partial_asr(tmp_path, monkeypatch):
    from bilibili_live_monitor import offline_asr
    root = tmp_path / 'media'
    root.mkdir()
    first = root / 'a-20300101T000005Z-b-00000.mkv'
    second = root / 'a-20300101T000005Z-b-00001.mkv'
    first.write_bytes(b'first')
    second.write_bytes(b'second')
    recover = False
    probes = []
    def probe(root, path):
        probes.append(path.name)
        failed = path == second and not recover
        stat = path.stat()
        return {'path': str(path), 'room_id': '123456', 'name': path.name,
                'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'sealed': 'probe_failed' if failed else 'sealed_readable', 'duration_sec': None if failed else 2}
    class Worker:
        def __init__(self, *a):
            self.model = 'synthetic'
        def start(self):
            pass
        def close(self):
            pass
        def request(self, *a):
            return {'text': 'synthetic'}
    monkeypatch.setattr(offline_asr, 'probe', probe)
    monkeypatch.setattr(offline_asr, 'Worker', Worker)
    monkeypatch.setattr(offline_asr, 'decode_pcm', lambda *a: struct.pack('<f', 0.1) * 32000)
    config = {'workspace': str(tmp_path), 'recording_root': str(root), 'start_utc': START,
              'recording_segment_seconds': 300, 'room_id': '123456', 'asr_python': 'synthetic', 'asr_model': 'synthetic'}
    state = {'cutoff': epoch(START) + 20}
    with pytest.raises(RuntimeError, match='尚不可读取'):
        run_offline(config, state)
    assert not (tmp_path / 'offline_segments.json').exists()
    recover = True
    probes.clear()
    destination = run_offline(config, state)
    from pathlib import Path
    assert len(json.loads(Path(destination).read_text())) == 2
    assert probes == [second.name]


@pytest.mark.parametrize('injected,valid', [
    (b'', True),
    (b'<script type="module" src="https://static.cloudflareinsights.com/beacon.min.js/v1" data-cf-beacon=\'{}\'></script>\n', True),
    (b'<script src="https://example.org/changed.js"></script>', False),
    (b'<script src="https://example.org/changed.js" data-src="https://static.cloudflareinsights.com/beacon.min.js/v1"></script>', False),
    (b'<script src="https://example.org/changed.js" src="https://static.cloudflareinsights.com/beacon.min.js/v1"></script>', False),
    (b'changed content', False),
])
def test_remote_verification_allows_only_known_cdn_beacon(tmp_path, monkeypatch, injected, valid):
    import io

    from bilibili_live_monitor.session_runner import verify_remote
    public = tmp_path / 'public'
    public.mkdir()
    (public / 'index.html').write_bytes(b'<html>content</html>')
    (public / 'snapshot.json').write_bytes(b'{}')
    def fetch(request, **kwargs):
        assert request.get_header('User-agent') == 'bilibili-live-monitor/0.1'
        return io.BytesIO(b'<html>content' + injected + b'</html>' if request.full_url.endswith('index.html') else b'{}')
    monkeypatch.setattr('urllib.request.urlopen', fetch)
    config = {'workspace': str(tmp_path), 'url': 'https://example.org/'}
    if valid:
        verify_remote(config)
    else:
        with pytest.raises(RuntimeError, match='不一致'):
            verify_remote(config)
