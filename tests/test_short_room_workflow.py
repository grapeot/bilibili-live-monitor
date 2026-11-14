from copy import deepcopy

import pytest
from test_session_health import config as health_config
from test_session_health import fake_api
from test_session_workflow import automation, settings

from bilibili_live_monitor import session_health as health
from bilibili_live_monitor import session_runner as runner
from bilibili_live_monitor.live_share import iso
from bilibili_live_monitor.live_share_control import load_config
from bilibili_live_monitor.live_share_watch import terminal
from bilibili_live_monitor.session_workflow import SessionWorkflows


def test_redirected_capture_keeps_workflow_identity_and_reads_actual_media(tmp_path):
    service, candidate = automation(tmp_path)
    service.store.db.execute('DELETE FROM rooms')
    service.store.room({'room_id': '654321', 'url': 'https://live.bilibili.com/654321',
                        'source_url': candidate['url'], 'status': 'capturing'})
    manager = SessionWorkflows(service, settings(tmp_path))
    record = manager.prepare('123456', candidate)
    original = deepcopy(record)
    path = manager.initialize(record)
    cfg = load_config(path)
    assert cfg['room_id'] == '654321'
    assert cfg['platform_room_id'] == '123456'
    assert cfg['recording_root'] == str(tmp_path / 'recordings/654321')
    assert cfg['workflow_key'] == record['key']
    assert record == original
    assert manager.initialize(record) == path
    assert manager.prepare('123456', candidate)['url'] == cfg['url']
    service.store.close()


def test_ambiguous_capture_alias_is_not_guessed(tmp_path):
    service, candidate = automation(tmp_path)
    service.store.db.execute('DELETE FROM rooms')
    for rid in ('654321', '654322'):
        service.store.room({'room_id': rid, 'source_url': candidate['url']})
    manager = SessionWorkflows(service, settings(tmp_path))
    with pytest.raises(ValueError, match='多个采集房间'):
        manager.initialize(manager.prepare('123456', candidate))
    service.store.close()


def test_delayed_redirect_waits_before_freezing_source(tmp_path):
    from bilibili_live_monitor.live_share_control import read_session_source
    from bilibili_live_monitor.session_offline import recording_files
    service, candidate = automation(tmp_path)
    service.store.db.execute('DELETE FROM rooms')
    manager = SessionWorkflows(service, settings(tmp_path))
    record = manager.prepare('123456', candidate)
    original = deepcopy(record)
    with pytest.raises(RuntimeError, match='采集来源尚未就绪'):
        manager.initialize(record)
    assert not (tmp_path / 'sessions' / record['key'] / 'config.json').exists()
    service.store.room({'room_id': '654321', 'url': 'https://live.bilibili.com/654321',
                        'source_url': candidate['url'], 'status': 'capturing'})
    service.store.db.execute('INSERT INTO segments(room_id,seq,data) VALUES(?,?,?)',
                            ('654321', 1, '{"seq":1,"text":"合成短号语音","start_utc":"2030-01-01T00:00:01Z"}'))
    service.store.db.commit()
    cfg = load_config(manager.initialize(record))
    assert cfg['room_id'] == '654321' and cfg['platform_room_id'] == '123456'
    assert read_session_source(cfg)['segments'][0]['text'] == '合成短号语音'
    assert record == original
    media = tmp_path / 'recordings/654321'
    media.mkdir(parents=True)
    sample = media / 'abc123-20300101T000001Z-def456-00000.wav'
    sample.write_bytes(b'synthetic-media')
    assert any(path == sample for files in recording_files(cfg, 1893456060).values()
               for _, path in files)
    platform = {'candidates': [{**candidate, 'live_status': 0, 'offline_checks': 2,
                              'status_checked_utc': iso(1893456100)}],
                'sources': [{'room_id': '654321', 'status': 'stopped'}]}
    assert terminal(cfg, platform, 1893456101)
    service.store.close()


def test_candidate_temporary_short_id_uses_stable_url(tmp_path):
    service, candidate = automation(tmp_path)
    service.store.db.execute('DELETE FROM rooms')
    service.store.room({'room_id': '654321', 'url': 'https://live.bilibili.com/654321',
                        'source_url': candidate['url'], 'status': 'capturing'})
    candidate['room_id'] = '654321'
    manager = SessionWorkflows(service, settings(tmp_path))
    cfg = load_config(manager.initialize(manager.prepare('123456', candidate)))
    assert cfg['room_id'] == '654321' and cfg['platform_room_id'] == '123456'
    service.store.close()


def test_alias_terminal_requires_canonical_offline_and_capture_stopped():
    cfg = {'room_id': '654321', 'platform_room_id': '123456', 'deadline': 2000,
           'live_start': '2030-01-01 08:00:00'}
    candidate = {'room_id': '123456', 'live_status': 0, 'offline_checks': 2,
                 'status_checked_utc': iso(999)}
    platform = {'candidates': [candidate], 'sources': [{'room_id': '654321', 'status': 'capturing'}]}
    assert not terminal(cfg, platform, 1000)
    platform['sources'][0]['status'] = 'stopped'
    assert terminal(cfg, platform, 1000)
    candidate['status_checked_utc'] = iso(100)
    assert not terminal(cfg, platform, 1000)


def test_alias_health_reads_capture_but_resumes_canonical_candidate(tmp_path, monkeypatch):
    cfg, calls = health_config(tmp_path), []
    cfg['platform_room_id'] = '654321'
    base = fake_api(cfg, calls)
    def request(url, data=None):
        result = base(url, data)
        if url.endswith('/api/platform'):
            result['candidates'][0]['room_id'] = '654321'
        return result
    monkeypatch.setattr(health, 'request', request)
    result = health.inspect(cfg, {}, 1000)
    assert result['recovery'] == 'room_resume_requested'
    assert any(url.endswith('/candidates/654321/resume') for url, _ in calls)
    assert not any(url.endswith('/candidates/123456/resume') for url, _ in calls)


def test_alias_sealing_waits_for_canonical_live_candidate(tmp_path, monkeypatch):
    cfg = {'workspace': str(tmp_path), 'room_id': '654321', 'platform_room_id': '123456',
           'content_mode': 'talk', 'ai_enabled': False, 'live_start': '2030-01-01 08:00:00',
           'api': 'http://synthetic', 'seal_seconds': 10}
    state = {'stage': 'sealing', 'cutoff': iso(500), 'media_signature': [], 'stable_since': 0}
    platform = {'candidates': [{'room_id': '123456', 'live_status': 1, 'live_start': cfg['live_start']}],
                'sources': [{'room_id': '654321', 'status': 'stopped'}]}
    monkeypatch.setattr(runner, 'recording_files', lambda *a: {})
    monkeypatch.setattr(runner.watch, 'request', lambda *a: platform)
    runner.advance(cfg, state, 1000)
    assert state['stage'] == 'sealing'
    platform['candidates'][0]['live_status'] = 0
    runner.advance(cfg, state, 1000)
    assert state['stage'] == 'offline_asr'
