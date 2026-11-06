import argparse
import hashlib
import json
import os
import time

import pytest

from bilibili_live_monitor import archive_media as media
from bilibili_live_monitor import offline_asr, session_backfill, session_offline
from bilibili_live_monitor import recording_archive as archive
from bilibili_live_monitor.live_share import epoch


def fixture(tmp_path, *, stage='completed'):
    root = tmp_path / 'sessions'
    scene = root / 'one'
    scene.mkdir(parents=True)
    source = tmp_path / 'recordings' / '123456' / 'abcdef-20260101T000000Z-abcd-00000.mkv'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'original video')
    os.utime(source, (time.time() - 60, time.time() - 60))
    row = {'path': str(source), **media.file_stat(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
           'duration_sec': 3, 'sealed': 'sealed_readable', 'room_id': '123456', 'name': source.name}
    (scene / 'config.json').write_text(json.dumps({'recording_root': str(source.parent)}))
    (scene / 'workflow.json').write_text(json.dumps({'stage': stage}))
    (scene / 'offline').mkdir()
    (scene / 'offline/manifest.json').write_text(json.dumps({'rows': [row]}))
    return root, source, row


def accepted(source, row):
    target = media.archive_path(source)
    target.parent.mkdir()
    target.write_bytes(b'hevc')
    saved = {'status': 'archived', 'source': str(source), 'source_stat': media.file_stat(source),
             'output': str(target), 'output_stat': media.file_stat(target), 'legacy_row': row,
             'source_info': {'timestamp_max_adjustment_us': 0}}
    media.receipt_path(source).write_text(json.dumps(saved))
    return target


def args(root, action='plan', **kwargs):
    return argparse.Namespace(action=action, sessions_root=root, session=[], max_files=1,
                              execute=False, permanent=False, **kwargs)


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('action', ['inspect', 'plan', 'run', 'cleanup'])
def test_preview_is_read_only(tmp_path, monkeypatch, action):
    root, _, _ = fixture(tmp_path)
    before = snapshot(tmp_path)
    monkeypatch.setattr(archive, 'command', lambda *_a, **_k: pytest.fail('preview started process'))
    assert archive.main(args(root, action)) == 0
    assert snapshot(tmp_path) == before


def test_live_and_changed_and_missing_are_not_encoded(tmp_path):
    root, source, _ = fixture(tmp_path, stage='live')
    assert archive.inventory(root) == []
    (root / 'one/workflow.json').write_text('{"stage":"completed"}')
    source.write_bytes(b'changed')
    assert archive.inventory(root)[0]['reason'] == 'changed_original'
    source.unlink()
    assert archive.inventory(root)[0]['reason'] == 'missing_original'


def test_archive_fallback_preserves_original_identity_and_scan(tmp_path, monkeypatch):
    root, source, row = fixture(tmp_path)
    target = accepted(source, row)
    source.unlink()
    monkeypatch.setattr(offline_asr, '_sha256', lambda _: pytest.fail('archive computed hash'))
    offline_asr._verify_identity(row)
    assert media.resolve_media(source) == target
    assert offline_asr.probe(source.parent, source)['sha256'] == row['sha256']
    assert media.source_paths(source.parent, {'.mkv'}) == [source]
    assert archive.inventory(root)[0]['action'] == 'archived'


def test_existing_changed_original_does_not_hide_behind_archive(tmp_path):
    _, source, row = fixture(tmp_path)
    accepted(source, row)
    source.write_bytes(b'changed original')
    with pytest.raises(ValueError, match='素材已变化'):
        offline_asr._verify_identity(row)


def test_changed_output_refuses_fallback(tmp_path):
    _, source, _ = fixture(tmp_path)
    target = accepted(source, {})
    source.unlink()
    target.write_bytes(b'tampered output')
    with pytest.raises(ValueError, match='归档副本'):
        media.resolve_media(source)


def test_cleanup_rechecks_before_deleting(tmp_path, monkeypatch):
    root, source, row = fixture(tmp_path)
    accepted(source, row)
    item = archive.inventory(root)[0]
    monkeypatch.setattr(archive, 'no_writer', lambda _: None)
    monkeypatch.setattr(archive, 'verify_output', lambda *_: (_ for _ in ()).throw(ValueError('broken')))
    with pytest.raises(ValueError, match='broken'):
        archive.cleanup(item, permanent=True)
    assert source.exists()
    monkeypatch.setattr(archive, 'verify_output', lambda *_: {})
    result = archive.cleanup(item, permanent=True)
    assert result['status'] == 'deleted'
    assert not source.exists()
    assert media.resolve_media(source).is_file()


def test_lock_survives_another_open_handle(tmp_path):
    with archive.writer_lock(tmp_path):
        with pytest.raises(BlockingIOError):
            with archive.writer_lock(tmp_path):
                pass


def test_decoding_archive_records_physical_input(tmp_path, monkeypatch):
    _, source, row = fixture(tmp_path)
    target = accepted(source, row)
    source.unlink()
    called = []
    monkeypatch.setattr(offline_asr, 'decode_pcm', lambda p: called.append(p) or b'\x00' * 6400)
    class Session:
        model = 'synthetic'
        def request(self, *_):
            return {'text': 'test', 'infer_ms': 1}
    result = offline_asr.transcribe_file(Session(), tmp_path, row, 30)
    assert called == [target]
    assert result['media_is_archived']
    assert result['sha256'] == row['sha256']
    assert result['media_input'] == str(target)


def test_profile_refuses_unapproved_media():
    with pytest.raises(ValueError):
        archive.check_profile({'streams': []})


@pytest.mark.parametrize('width,height,rate', [(1920, 1440, '60/1'), (1920, 1080, '30/1'),
    (1600, 1280, '30/1'), (1280, 720, '30/1'), (1600, 1280, '60/1'),
    (960, 720, '30/1'), (900, 720, '30/1'), (1600, 1280, '30000/1001'),
    (1920, 1080, '30000/1001')])
def test_historical_profiles_keep_original_size_and_rate(width, height, rate):
    archive.check_profile({'streams': [{'codec_type': 'video', 'codec_name': 'h264',
        'width': width, 'height': height, 'pix_fmt': 'yuv420p', 'avg_frame_rate': rate,
        'color_space': 'bt709', 'color_transfer': 'bt709', 'color_primaries': 'bt709'},
        {'codec_type': 'audio', 'codec_name': 'aac'}]})


@pytest.mark.parametrize('override', [{'width': 3840}, {'height': 2160}, {'width': 901},
    {'avg_frame_rate': '120/1'}, {'pix_fmt': 'yuv420p10le'}, {'color_transfer': 'smpte2084'}])
def test_unvalidated_profile_is_rejected(override):
    video = {'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080,
             'pix_fmt': 'yuv420p', 'avg_frame_rate': '30/1', 'color_space': 'bt709',
             'color_transfer': 'bt709', 'color_primaries': 'bt709'}
    with pytest.raises(ValueError):
        archive.check_profile({'streams': [{**video, **override}, {'codec_type': 'audio', 'codec_name': 'aac'}]})


def test_verify_accepts_container_rate_estimates_and_rejects_dropped_frames(monkeypatch, tmp_path):
    video = {'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080,
             'pix_fmt': 'yuv420p', 'avg_frame_rate': '30/1', 'r_frame_rate': '30/1',
             'nb_read_frames': '182', 'color_space': 'bt709', 'color_transfer': 'bt709',
             'color_primaries': 'bt709'}
    audio = {'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2, 'sample_rate': '48000'}
    source = {'streams': [video, audio], 'format': {'duration': '10.193'}, 'timestamp_max_adjustment_us': 0}
    output_video = {**video, 'codec_name': 'hevc', 'avg_frame_rate': '1092/61', 'r_frame_rate': '60/1'}
    output = {'streams': [output_video, audio], 'format': {'duration': '10.2'}}
    monkeypatch.setattr(archive, 'probe', lambda *_a, **_k: output)
    monkeypatch.setattr(archive, 'command', lambda *_a, **_k: None)
    archive.verify_output(source, tmp_path / 'output.mp4')
    output_video['nb_read_frames'] = '181'
    with pytest.raises(ValueError, match='帧数改变'):
        archive.verify_output(source, tmp_path / 'output.mp4')
    video.pop('nb_read_frames')
    with pytest.raises(ValueError, match='无法验证原片解码帧数'):
        archive.verify_output(source, tmp_path / 'output.mp4')


def test_timestamp_adjustment_bounds_duplicates_and_backwards_jump(monkeypatch, tmp_path):
    assert archive.timestamp_adjustment(['0', '0', '0.01']) == 1
    assert archive.timestamp_adjustment(['0', '2', '1', '3']) == 1_000_001
    assert archive.timestamp_adjustment(['0'] * 1002) == 1001
    monkeypatch.setattr(archive, 'probe', lambda *_a, **_k: pytest.fail('invalid source reached output probe'))
    for correction in (1001, 1_000_001):
        with pytest.raises(ValueError, match='超过1毫秒'):
            archive.verify_output({'timestamp_max_adjustment_us': correction}, tmp_path / 'output.mp4')


def test_only_own_manifest_recovered_source_is_eligible(tmp_path):
    root, source, row = fixture(tmp_path)
    recovered = root / 'one/offline/recovered' / source.name
    recovered.parent.mkdir()
    source.rename(recovered)
    row['path'] = str(recovered)
    manifest = root / 'one/offline/manifest.json'
    manifest.write_text(json.dumps({'rows': [row]}))
    assert archive.inventory(root)[0]['action'] == 'encode'
    foreign = root / 'other/offline/recovered' / source.name
    foreign.parent.mkdir(parents=True)
    recovered.rename(foreign)
    row['path'] = str(foreign)
    manifest.write_text(json.dumps({'rows': [row]}))
    assert archive.inventory(root)[0]['reason'] == 'external_media'


def test_bad_old_receipt_does_not_break_new_scene_listing(tmp_path):
    _, source, row = fixture(tmp_path)
    target = accepted(source, row)
    source.unlink()
    target.write_bytes(b'bad archive changed')
    cfg = {'recording_root': str(source.parent), 'start_utc': '2026-11-05T00:00:00Z'}
    assert session_offline.recording_files(cfg, epoch('2026-11-05T01:00:00Z')) == {}
    cfg['start_utc'] = '2026-01-01T00:00:00Z'
    paths = session_offline.recording_files(cfg, epoch('2026-01-01T01:00:00Z'))
    assert paths
    with pytest.raises(ValueError, match='归档副本'):
        offline_asr.probe(source.parent, source)
    media.receipt_path(source).write_text('broken json')
    cfg['start_utc'] = '2026-11-05T00:00:00Z'
    assert session_offline.recording_files(cfg, epoch('2026-11-05T01:00:00Z')) == {}


def test_archived_backfill_uses_original_time(tmp_path):
    _, source, row = fixture(tmp_path)
    cutoff = epoch('2026-01-01T01:00:00Z')
    os.utime(source, (cutoff - 60, cutoff - 60))
    row.update(media.file_stat(source))
    accepted(source, row)
    source.unlink()
    plan = session_backfill.prepare({'recording_root': str(source.parent), 'room_id': '123456',
        'start_utc': '2026-01-01T00:00:00Z', 'recording_segment_seconds': 300}, cutoff)
    assert not plan['skipped']
    assert plan['rows'][0]['sha256'] == row['sha256']
    assert plan['rows'][0]['wall_start'] == epoch('2026-01-01T00:00:00Z')


def test_archive_rejects_wrong_old_source_identity(tmp_path):
    _, source, row = fixture(tmp_path)
    accepted(source, row)
    source.unlink()
    assert media.resolve_media(source, expected_sha=row['sha256']).is_file()
    with pytest.raises(ValueError, match='旧来源不一致'):
        media.resolve_media(source, expected_sha='0' * 64)
    with pytest.raises(ValueError, match='旧来源不一致'):
        offline_asr._verify_identity({**row, 'sha256': '0' * 64})
