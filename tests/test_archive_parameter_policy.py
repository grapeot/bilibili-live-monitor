import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from test_media_codec import PREFIX
from test_media_codec import capture as capture
from test_media_codec import source as source

from bilibili_live_monitor import archive_policy as policy
from bilibili_live_monitor import archive_validation as validation
from bilibili_live_monitor import media_codec as codec
from bilibili_live_monitor import recording_archive as archive
from bilibili_live_monitor.archive_media import file_stat, receipt_path


def test_blocked_parameters_retry_on_material_or_policy_change(source, monkeypatch):
    item = {'source': str(source), 'source_stat': file_stat(source)}
    monkeypatch.setattr(archive, 'check_source', lambda *_: None)
    saved = archive.retain_original(item, archive.ArchiveSkip('codec_missing_parameter_history', 'missing'))
    assert policy.reusable_skip(saved, item)
    previous = source.with_name(f'{PREFIX}-00000.mkv')
    previous.rename(previous.with_suffix('.hidden'))
    assert not policy.reusable_skip(saved, item)
    previous.with_suffix('.hidden').rename(previous)
    assert policy.reusable_skip(saved, item)
    receipt_path(previous).parent.mkdir(exist_ok=True)
    receipt_path(previous).write_text('{}')
    assert not policy.reusable_skip(saved, item)
    receipt_path(previous).unlink()
    assert policy.reusable_skip(saved, item)
    monkeypatch.setattr(codec, 'POLICY', 'future-parameters')
    assert not policy.reusable_skip(saved, item)
    assert source.exists()


def test_old_parameter_skip_retries_but_low_bitrate_receipt_stays_valid(source):
    item = {'source': str(source), 'source_stat': file_stat(source)}
    old = {**item, 'policy': policy.ARCHIVE_POLICY, 'status': 'skipped',
           'codec_policy': validation.POLICY, 'reason': 'source_decode_error'}
    assert not policy.reusable_skip(old, item)
    for reason in ('unsupported_profile', 'unsupported_streams', 'source_timestamps', 'invalid_duration'):
        assert not policy.reusable_skip({**old, 'reason': reason}, item)
    assert policy.reusable_skip({**old, 'reason': 'low_bitrate'}, item)


def test_io_failure_is_not_a_parameter_block(source, monkeypatch):
    monkeypatch.setattr(codec, '_frame_probe', lambda *_: SimpleNamespace(
        returncode=1, stderr=b'Input/output error', stdout=b'{}'))
    with pytest.raises(subprocess.CalledProcessError):
        codec._valid_candidate(source)
    assert not policy.decode_error(subprocess.CalledProcessError(1, 'ffmpeg', stderr=b'permission denied; invalid NAL unit'))


@pytest.mark.parametrize('returncode,stderr', [(-9, b''), (-15, b'invalid NAL unit'),
                                               (1, b''), (1, b'unknown probe failure')])
def test_probe_execution_failure_never_excludes_one_parameter_explanation(source, monkeypatch, returncode, stderr):
    report = codec.inspect_video(source)
    monkeypatch.setattr(codec, '_copy_with_header', lambda *_: None)
    monkeypatch.setattr(codec, '_leading_parameters', lambda *_: None)
    monkeypatch.setattr(codec, '_frame_probe', lambda *_: SimpleNamespace(
        returncode=returncode, stderr=stderr, stdout=b'{}'))
    with pytest.raises(subprocess.CalledProcessError):
        codec._select_candidate(source, source.parent / 'unused.mkv', report)


def test_preflight_skip_invalidates_when_parameter_dependencies_change(source, monkeypatch):
    item = {'source': str(source), 'source_stat': file_stat(source)}
    skip = archive.ArchiveSkip('unsupported_profile', 'unsupported')
    skip.dependencies = policy.dependency_fingerprint(source)
    monkeypatch.setattr(archive, 'check_source', lambda *_: None)
    saved = archive.retain_original(item, skip)
    assert policy.reusable_skip(saved, item)
    source.with_name(f'{PREFIX}-00000.mkv').rename(source.parent / 'hidden.mkv')
    assert not policy.reusable_skip(saved, item)


def test_changed_dependencies_do_not_create_sticky_skip(source, monkeypatch):
    item = {'source': str(source), 'source_stat': file_stat(source)}
    skip = archive.ArchiveSkip('codec_ambiguous_parameters', 'ambiguous')
    skip.dependencies = policy.dependency_fingerprint(source)
    source.with_name(f'{PREFIX}-00000.mkv').rename(source.parent / 'hidden.mkv')
    monkeypatch.setattr(archive, 'check_source', lambda *_: None)
    with pytest.raises(ValueError, match='参数依赖改变'):
        archive.retain_original(item, skip)
    assert not policy.skip_path(source).exists()


def test_prepare_video_can_grow_codec_private_and_keep_packets(source, monkeypatch):
    before = codec._sha(source)
    report = codec.inspect_video(source)
    report['initial_avcc'] += '00' * 16
    monkeypatch.setattr(codec, 'inspect_video', lambda *_: report.copy())
    output, parameters = codec.prepare_video(source)
    assert parameters['parameter_selection']['history_decode_ok']
    assert codec._decode(output)
    assert codec._packets(output) == codec._packets(source)
    assert codec._sha(source) == before


def test_self_contained_first_packet_does_not_require_missing_predecessor(capture, tmp_path):
    source = tmp_path / f'{PREFIX}-00003.mkv'
    shutil.copyfile(capture / 'normal.mkv', source)
    assert codec._leading_parameters(source, codec._scan_file(source)[0]) is not None
    report = codec.inspect_video(source)
    assert report['dependencies'] == []
    assert codec.prepare_video(source)[0] == source


def test_self_contained_first_packet_does_not_consult_corrupt_history(capture, tmp_path, monkeypatch):
    source = tmp_path / f'{PREFIX}-00003.mkv'
    shutil.copyfile(capture / 'normal.mkv', source)
    def bad_history(*_):
        raise AssertionError('self-contained segment must not consult damaged history')
    monkeypatch.setattr(codec, '_history', bad_history)
    assert codec.inspect_video(source)['dependencies'] == []


def test_native_reference_uses_decoded_size_instead_of_stale_container():
    frames = [{'media_type': 'video', 'width': 1600, 'height': 1280, 'pix_fmt': 'yuv420p',
               'sample_aspect_ratio': '1:1', 'best_effort_timestamp_time': '0',
               'color_space': 'bt709', 'color_transfer': 'bt709', 'color_primaries': 'bt709'}]
    info = {'streams': [{'codec_type': 'video', 'width': 1920, 'height': 1080}],
            'video_evidence': validation.video_evidence(frames)}
    before = json.dumps(info)
    reference = validation.normalize_reference(info, mixed=False)
    assert (reference['streams'][0]['width'], reference['streams'][0]['height']) == (1600, 1280)
    assert json.dumps(info) == before
