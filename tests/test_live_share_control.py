import argparse
import hashlib
import json
import sqlite3

import pytest

from bilibili_live_monitor.live_share import atomic_json, epoch, fingerprint, followup_candidates
from bilibili_live_monitor.live_share_control import (
    configure_parser,
    export_snapshot,
    initialize,
    load_config,
    validate_config,
    verify_snapshot,
)
from bilibili_live_monitor.song_sources import register_source, verified_titles

START = '2030-01-01T12:00:00+08:00'
UTC_START = '2030-01-01T04:00:00+00:00'
END = '2030-01-01T04:02:00+00:00'


def args_for(tmp_path, *extra):
    database = tmp_path / 'source.sqlite3'
    if not database.exists():
        with sqlite3.connect(database) as db:
            db.executescript('''
                CREATE TABLE raw_events(id INTEGER, room_id TEXT, received_utc TEXT, command TEXT, data TEXT);
                CREATE TABLE timeline(id INTEGER, room_id TEXT, kind TEXT, at_utc TEXT, data TEXT);
                CREATE TABLE segments(seq INTEGER, room_id TEXT, data TEXT);
            ''')
    parser = argparse.ArgumentParser()
    configure_parser(parser.add_subparsers(dest='command'))
    return parser.parse_args(['live-share', 'init', '--workspace', str(tmp_path / 'session'),
                              '--database', str(database), '--room', '123456', '--label', '合成直播',
                              '--start', START, *extra])


def initialized(tmp_path):
    args = args_for(tmp_path)
    initialize(args)
    return load_config(args.workspace / 'config.json')


def write_source(tmp_path, title='测试歌曲', body='已核验的测试歌词'):
    path = tmp_path / 'search.json'
    atomic_json(path, {'data': {'results': [
        {'title': title + ' - 歌词', 'url': 'https://example.org/music', 'raw_content': body}]}})
    return path


def test_init_defaults_are_local_only_and_use_real_timezone(tmp_path, monkeypatch):
    import subprocess
    import urllib.request
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('unexpected process'))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('unexpected network'))
    args = args_for(tmp_path)
    result = initialize(args)
    config = load_config(args.workspace / 'config.json')
    assert config['start_utc'] == UTC_START
    assert config['live_start'] == '2030-01-01 12:00:00'
    assert config['deadline'] == epoch(UTC_START) + 43200
    assert not config['ai_enabled'] and config['remote'] is None
    assert not result['started'] and not result['published']
    assert not (args.workspace / 'state.json').exists()
    with pytest.raises(ValueError, match='非空'):
        initialize(args)


@pytest.mark.parametrize('extra', [('--hours', '13'), ('--hours', 'nan'),
                                  ('--ai-session', 'example'), ('--remote', 'example:/site/')])
def test_bad_init_does_not_create_workspace(tmp_path, extra):
    args = args_for(tmp_path, *extra)
    with pytest.raises(ValueError):
        initialize(args)
    assert not args.workspace.exists()


def test_validation_rejects_different_broadcast_and_public_database(tmp_path):
    config = initialized(tmp_path)
    with pytest.raises(ValueError, match='北京时间'):
        validate_config({**config, 'live_start': '2030-01-01 04:00:00'})
    with pytest.raises(ValueError, match='数据库'):
        validate_config({**config, 'database': str(tmp_path / 'session/public/private.sqlite3')})


def test_workflow_finalizing_snapshot_is_valid_after_live_collection_stops(tmp_path):
    config = initialized(tmp_path)
    workspace = tmp_path / 'session'
    atomic_json(workspace / 'state.json', {'phase': 'completed', 'cutoff': epoch(END)})
    export_snapshot(config)
    path = workspace / 'public/snapshot.json'
    snapshot = json.loads(path.read_text())
    snapshot['phase'] = 'finalizing'
    atomic_json(path, snapshot)
    assert 'completed_page_still_live' in verify_snapshot(config)['errors']
    config['workflow_key'] = 'synthetic-workflow'
    assert verify_snapshot(config)['valid']


def test_ai_interpreter_preserves_venv_symlink(tmp_path):
    base = tmp_path / 'base-python'
    base.touch()
    binary = tmp_path / 'venv/bin/python'
    binary.parent.mkdir(parents=True)
    binary.symlink_to(base)
    args = args_for(tmp_path, '--ai-session', 'synthetic-session', '--model', 'provider/model',
                    '--opencode-cwd', str(tmp_path), '--opencode-python', str(binary))
    initialize(args)
    config = load_config(args.workspace / 'config.json')
    assert config['ai_enabled']
    assert config['opencode_python'] == str(binary.absolute())
    assert config['opencode_python'] != str(base)


def test_source_registration_is_idempotent_and_skips_bad_payload(tmp_path):
    config = initialized(tmp_path)
    workspace = tmp_path / 'session'
    source = write_source(tmp_path)
    first = register_source(workspace, '测试歌曲', source, 0)
    assert register_source(workspace, '测试歌曲', source, 0) == first
    assert len(json.loads((workspace / 'verified_songs.json').read_text())) == 1
    assert verified_titles(workspace) == ('测试歌曲',)
    with pytest.raises(ValueError):
        register_source(workspace, '私人信息', source, 0)
    assert verified_titles(workspace) == ('测试歌曲',)
    assert not config['ai_enabled']


def test_second_host_rules_use_original_blind_box_and_guard_setting():
    def gift(ident, original):
        return ('gift', UTC_START, {'sender_uid': '12', 'author': '合成观众',
                'gift': {'event_type': 'SEND_GIFT_V2', 'event_id': ident, 'num': 1,
                         'gift_name': '抽中测试奖品', 'gift_id': '9',
                         'blind_gift': {'original_gift_name': original}}})
    source = {'items': [gift('one', '甲盲盒'), gift('two', '乙盲盒')], 'raw': [], 'segments': []}
    a = followup_candidates(source, UTC_START, {'blind_gift_names': ['甲盲盒']})
    b = followup_candidates(source, UTC_START, {'blind_gift_names': ['乙盲盒']})
    assert a[0]['blind_orders'] == b[0]['blind_orders'] == 1
    assert followup_candidates(source, UTC_START, {'include_blind': False}) == []
    source['items'] = [('gift', UTC_START, {'sender_uid': '12', 'author': '合成舰长', 'guard_level': 3,
                       'gift': {'event_type': 'SEND_GIFT_V2', 'event_id': 'three', 'num': 1,
                                'gift_name': '粉丝团灯牌', 'gift_id': '31164'}})]
    assert followup_candidates(source, UTC_START) == []
    assert len(followup_candidates(source, UTC_START, {'exclude_guard_lamps': False})) == 1


def frozen_fixture(tmp_path):
    config = initialized(tmp_path)
    segment = {'seq': 1, 'start_utc': UTC_START, 'end_utc': END, 'text': '私有转写内容', 'is_final': True}
    later = {**segment, 'seq': 2, 'start_utc': END, 'end_utc': '2030-01-01T04:03:00+00:00'}
    with sqlite3.connect(config['database']) as db:
        db.executemany('INSERT INTO segments VALUES (?, ?, ?)',
                       [(s['seq'], '123456', json.dumps(s)) for s in (segment, later)])
    atomic_json(tmp_path / 'session/state.json', {'phase': 'completed', 'cutoff': epoch(END)})
    atomic_json(tmp_path / 'session/annotations.json', {'reviews': [
        {'seq': 1, 'fingerprint': fingerprint(segment), 'song': '测试歌曲', 'label': 'singing',
         'music_source': 'probable_live', 'confidence': 'high'}]})
    return config


def test_frozen_export_and_registration_fix_actual_visible_title(tmp_path, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('unexpected network'))
    config = frozen_fixture(tmp_path)
    before = hashlib.sha256((tmp_path / 'source.sqlite3').read_bytes()).hexdigest()
    result = export_snapshot(config)
    assert not result['published'] and result['phase'] == 'completed'
    report = verify_snapshot(config)
    assert report['valid'] and report['unpublished_songs'] == ['测试歌曲']
    assert report['unreviewed_windows'] == 0  # next broadcast/time range excluded
    register_source(tmp_path / 'session', '测试歌曲', write_source(tmp_path), 0)
    export_snapshot(config)
    report = verify_snapshot(config)
    assert report['valid'] and report['unpublished_songs'] == []
    page = (tmp_path / 'session/public/index.html').read_text()
    assert '测试歌曲' in page and '私有转写内容' not in page and '本场已结束' in page
    assert hashlib.sha256((tmp_path / 'source.sqlite3').read_bytes()).hexdigest() == before


def test_verify_rejects_private_fields_in_projection(tmp_path):
    config = frozen_fixture(tmp_path)
    export_snapshot(config)
    path = tmp_path / 'session/public/snapshot.json'
    data = json.loads(path.read_text())
    data['debug'] = {'sender_uid': 'private'}
    atomic_json(path, data)
    report = verify_snapshot(config)
    assert not report['valid'] and 'private_field:sender_uid' in report['errors']


def test_request_only_title_is_detected_and_becomes_visible_after_registration(tmp_path):
    from bilibili_live_monitor.live_share import followup_evidence, read_source

    config = frozen_fixture(tmp_path)
    gift = {'sender_uid': '12', 'author': '合成观众', 'gift': {'event_type': 'SEND_GIFT_V2',
            'event_id': 'one', 'num': 1, 'gift_id': '31164', 'gift_name': '粉丝团灯牌'}}
    with sqlite3.connect(config['database']) as db:
        db.execute('INSERT INTO timeline VALUES (?, ?, ?, ?, ?)', (1, '123456', 'gift', UTC_START, json.dumps(gift)))
    source = read_source(tmp_path / 'source.sqlite3', '123456', UTC_START)
    annotations_path = tmp_path / 'session/annotations.json'
    annotations = json.loads(annotations_path.read_text())
    annotations['reviews'][0]['song'] = None
    annotations['song_requests'] = [{'id': followup_candidates(source, UTC_START)[0]['id'],
        'song': '测试歌曲', 'stage': 'accepted', 'evidence': [followup_evidence(source)[0]['id']]}]
    atomic_json(annotations_path, annotations)
    export_snapshot(config)
    assert verify_snapshot(config)['unpublished_songs'] == ['测试歌曲']
    register_source(tmp_path / 'session', '测试歌曲', write_source(tmp_path), 0)
    export_snapshot(config)
    assert verify_snapshot(config)['unpublished_songs'] == []
    public = json.loads((tmp_path / 'session/public/snapshot.json').read_text())
    assert public['followups'][0]['request_song'] == '测试歌曲'


def test_existing_song_source_is_not_truncated_during_duplicate_registration(tmp_path, monkeypatch):
    initialized(tmp_path)
    workspace = tmp_path / 'session'
    source = write_source(tmp_path)
    entry = register_source(workspace, '测试歌曲', source, 0)
    destination = workspace / 'song_sources' / entry['file']
    original = type(destination).write_bytes
    def observe_write(path, data):
        assert path != destination, 'must not truncate a source visible to concurrent readers'
        assert verified_titles(workspace) == ('测试歌曲',)
        return original(path, data)
    monkeypatch.setattr(type(destination), 'write_bytes', observe_write)
    register_source(workspace, '测试歌曲', source, 0)
    assert verified_titles(workspace) == ('测试歌曲',)
