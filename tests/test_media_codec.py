"""真实合成码流复现：参数切换 + 下一分片过期CodecPrivate。"""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bilibili_live_monitor import media_codec as codec
from bilibili_live_monitor.archive_media import archive_path, file_stat, resolve_media, source_paths

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='需要ffmpeg')
PREFIX = '01234567-20300101T000000Z-89abcdef'


def run(args):
    return subprocess.run(args, check=True, capture_output=True, timeout=30)


@pytest.fixture(scope='module')
def capture(tmp_path_factory):
    root = tmp_path_factory.mktemp('codec-capture')
    for label, size, duration in [('old', '192x128', '1'), ('new', '160x128', '4')]:
        run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=30',
             '-t', duration, '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
             '-g', '30', '-bf', '0', '-x264-params', 'colorprim=bt709:transfer=bt709:colormatrix=bt709',
             '-color_primaries', 'bt709', '-color_trc', 'bt709',
             '-colorspace', 'bt709', '-f', 'h264', str(root / f'{label}.h264')])
    (root / 'joined.h264').write_bytes((root / 'old.h264').read_bytes() + (root / 'new.h264').read_bytes())
    run(['ffmpeg', '-v', 'error', '-fflags', '+genpts', '-r', '30', '-i', str(root / 'joined.h264'),
         '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '5',
         '-c:v', 'copy', '-r', '30', '-c:a', 'aac', '-color_primaries', 'bt709', '-color_trc', 'bt709',
         '-colorspace', 'bt709', '-f', 'segment', '-segment_time', '2',
         '-reset_timestamps', '1', '-segment_format', 'matroska', str(root / f'{PREFIX}-%05d.mkv')])
    # Obtain an old header from a genuinely old single-resolution stream.
    run(['ffmpeg', '-v', 'error', '-r', '30', '-i', str(root / 'old.h264'),
         '-c', 'copy', str(root / 'old.mkv')])
    old, _, _ = codec._scan_file(root / 'old.mkv')
    target = root / f'{PREFIX}-00001.mkv'
    clean = root / 'clean.mkv'
    run(['ffmpeg', '-v', 'error', '-i', str(target), '-c', 'copy', '-bsf:v',
         'filter_units=remove_types=7|8', str(clean)])
    new, units, _ = codec._scan_file(clean)
    assert not units
    assert len(old) == len(new), (len(old), len(new))
    data = clean.read_bytes()
    assert data[:65536].count(new) == 1
    target.write_bytes(data[:65536].replace(new, old, 1) + data[65536:])
    assert not codec._decode(target, short=True)
    run(['ffmpeg', '-v', 'error', '-r', '30', '-i', str(root / 'new.h264'),
         '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
         '-c:v', 'copy', '-c:a', 'aac', str(root / 'normal.mkv')])
    return root


@pytest.fixture
def source(capture, tmp_path):
    for number in (0, 1):
        shutil.copyfile(capture / f'{PREFIX}-{number:05}.mkv', tmp_path / f'{PREFIX}-{number:05}.mkv')
    return tmp_path / f'{PREFIX}-00001.mkv'


def test_inspect_is_readonly_and_recovers_exact_packets(source):
    before = codec._sha(source)
    listing = set(source.parent.iterdir())
    report = codec.inspect_video(source)
    assert report['status'] == 'repairable'
    assert set(source.parent.iterdir()) == listing
    output, saved = codec.prepare_video(source, expected_sha=before)
    assert output != source and output.is_file()
    assert saved['status'] == 'repairable'
    assert codec._decode(output)
    assert codec._packets(source) == codec._packets(output)
    assert codec._sha(source) == before
    assert codec.prepare_video(source)[0] == output
    assert output not in source_paths(source.parent, {'.mkv'}, recursive=True)
    assert resolve_media(source) == source  # audio/metadata readers stay read-only
    assert resolve_media(source, video=True, expected_sha=before) == output


def test_mixed_segment_is_retained_and_not_encoded(source):
    transition = source.with_name(f'{PREFIX}-00000.mkv')
    assert codec.inspect_video(transition)['status'] == 'mixed_parameters'
    with pytest.raises(codec.CodecParametersError, match='分片内部'):
        codec.prepare_video(transition)
    assert not (source.parent / '.codec-media').exists()


def test_unmanaged_normal_and_wrong_sha(source, capture):
    normal = source.parent / 'plain.mkv'
    shutil.copyfile(capture / 'normal.mkv', normal)
    assert codec.prepare_video(normal)[0] == normal
    with pytest.raises(ValueError, match='指纹'):
        codec.prepare_video(source, expected_sha='0' * 64)
    normal_recorder = source.parent / 'aaaaaaaa-20300101T000000Z-bbbbbbbb-00000.mkv'
    shutil.copyfile(capture / 'normal.mkv', normal_recorder)
    assert codec.inspect_video(normal_recorder)['status'] == 'unchanged'
    assert codec.prepare_video(normal_recorder)[0] == normal_recorder


def test_missing_history_corrupt_cache_and_source_change(source):
    previous = source.with_name(f'{PREFIX}-00000.mkv')
    hidden = previous.with_suffix('.hidden')
    previous.rename(hidden)
    with pytest.raises(ValueError, match='前序参数'):
        codec.prepare_video(source)
    hidden.rename(previous)
    output, _ = codec.prepare_video(source)
    output.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='缓存校验'):
        codec.prepare_video(source)
    # A changed original must not reuse the old cache under an expected old SHA.
    expected = codec._sha(source)
    with source.open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ValueError, match='指纹'):
        codec.prepare_video(source, expected_sha=expected)


def test_archived_predecessor_supplies_parameters(source, capture):
    previous = source.with_name(f'{PREFIX}-00000.mkv')
    # A preceding *constant epoch* may already have been archived and removed.
    header, _, _ = codec._scan_file(capture / 'normal.mkv')
    target = archive_path(previous)
    target.parent.mkdir()
    target.write_bytes(b'accepted-archive-fixture')
    target.with_suffix('.json').write_text(json.dumps({
        'status': 'archived', 'source': str(previous), 'output': str(target),
        'output_stat': file_stat(target),
        'codec_parameters': {'policy': codec.POLICY, 'final_avcc': header.hex()},
    }))
    previous.unlink()
    output, _ = codec.prepare_video(source)
    assert codec._decode(output)


def test_archive_uses_normalized_source_and_keeps_original_identity(source, monkeypatch):
    from bilibili_live_monitor import recording_archive as archive
    output, parameters = codec.prepare_video(source)
    monkeypatch.setattr(archive, 'check_source', lambda item: None)
    monkeypatch.setattr(archive, 'check_profile', lambda info: None)
    observed = []
    def probe(path, **kwargs):
        observed.append(Path(path))
        return {'timestamp_max_adjustment_us': 0, 'format': {'duration': '2'}}
    monkeypatch.setattr(archive, 'probe', probe)
    monkeypatch.setattr(archive.validation, 'audio_evidence', lambda *_: {})
    monkeypatch.setattr(archive.validation, 'normalize_reference', lambda info, **_: info)
    monkeypatch.setattr(archive.shutil, 'disk_usage', lambda p: SimpleNamespace(free=10**12))
    monkeypatch.setattr(archive, 'verify_output', lambda info, path: {'format': {'duration': '2'}})
    commands = []
    def command(args, **kwargs):
        commands.append(args)
        Path(args[-1]).write_bytes(b'hevc')
    monkeypatch.setattr(archive, 'command', command)
    item = {'source': str(source), 'output': str(archive_path(source)),
            'source_stat': file_stat(source), 'legacy_row': {'sha256': codec._sha(source)}}
    archive.encode(item, ())
    assert observed == [output, output]
    assert commands[0][commands[0].index('-i') + 1] == str(output)
    receipt = json.loads(archive_path(source).with_suffix('.json').read_text())
    assert receipt['source'] == str(source) and receipt['source_stat'] == file_stat(source)
    assert receipt['codec_parameters'] == parameters
    assert receipt['media_input'] == str(output)
    assert receipt['legacy_row']['sha256'] == codec._sha(source)


def test_silent_wrong_height_must_not_bypass_parameter_history(tmp_path):
    # R1: old SPS can decode successfully while silently losing picture rows.
    for label, size in [('old', '192x128'), ('new', '192x144')]:
        run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=30',
             '-t', '2', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '0', '-bf', '0',
             '-c:a', 'aac', str(tmp_path / f'{label}.mkv')])
    previous = tmp_path / f'{PREFIX}-00000.mkv'
    # Strip repeated in-band headers so the stale container SPS determines decode.
    clean = tmp_path / 'new-clean.mkv'
    run(['ffmpeg', '-v', 'error', '-i', str(tmp_path / 'new.mkv'), '-c', 'copy',
         '-bsf:v', 'filter_units=remove_types=7|8', str(clean)])
    shutil.copyfile(clean, previous)
    old, _, _ = codec._scan_file(tmp_path / 'old.mkv')
    new, _, _ = codec._scan_file(clean)
    source = tmp_path / f'{PREFIX}-00001.mkv'
    data = clean.read_bytes()
    source.write_bytes(codec._patch_header(data[:65536], new, old) + data[65536:])
    assert codec._decode(source)  # no error, despite the wrong height
    assert codec.inspect_video(source)['status'] == 'repairable'
    output, report = codec.prepare_video(source)
    info = json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(output)]).stdout)
    assert info['streams'][0]['height'] == 144
    assert report['final_avcc'] != old.hex()
    assert codec._packets(source) == codec._packets(output)


def test_low_disk_does_not_create_recovery_cache(source, monkeypatch):
    monkeypatch.setattr(codec.shutil, 'disk_usage', lambda path: SimpleNamespace(free=0))
    with pytest.raises(ValueError, match='空间不足'):
        codec.prepare_video(source)
    assert not (source.parent / '.codec-media').exists()


def test_long_constant_capture_can_find_first_anchor_beyond_32_segments(tmp_path, capture, monkeypatch):
    header, _, _ = codec._scan_file(capture / 'normal.mkv')
    for number in range(41):
        (tmp_path / f'{PREFIX}-{number:05}.mkv').write_bytes(b'constant-epoch')
    monkeypatch.setattr(codec, '_scan_file', lambda p: (header, (), codec._stat(p)))
    monkeypatch.setattr(codec, '_decode', lambda *_a, **_k: True)
    report = codec.inspect_video(tmp_path / f'{PREFIX}-00040.mkv')
    assert report['status'] == 'unchanged'
    assert len(report['dependencies']) == 40


def test_adaptive_preparation_restores_initial_epoch_without_removing_later_changes(source, capture):
    # Previous first segment ends in the new epoch. This segment changes back
    # and forth, but its container deliberately carries the old initial header.
    joined = source.parent / 'back-and-forth.h264'
    joined.write_bytes((capture / 'new.h264').read_bytes() + (capture / 'old.h264').read_bytes()
                       + (capture / 'new.h264').read_bytes())
    fresh = source.parent / 'fresh.mkv'
    run(['ffmpeg', '-v', 'error', '-fflags', '+genpts', '-r', '30', '-i', str(joined),
         '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '9',
         '-c:v', 'copy', '-c:a', 'aac', str(fresh)])
    old, _, _ = codec._scan_file(capture / 'old.mkv')
    new, _, _ = codec._scan_file(fresh)
    data = fresh.read_bytes()
    source.write_bytes(codec._patch_header(data[:65536], new, old) + data[65536:])
    original = codec._sha(source)
    with pytest.raises(codec.CodecParametersError, match='分片内部'):
        codec.prepare_video(source)
    output, report = codec.prepare_video(source, expected_sha=original, allow_mixed=True)
    assert output != source and report['status'] == 'mixed_parameters'
    assert codec._packets(output) == codec._packets(source)
    assert codec._sha(source) == original
    frames = json.loads(run(['ffprobe', '-v', 'error', '-show_frames', '-select_streams', 'v:0',
                             '-show_entries', 'frame=width,height', '-of', 'json', str(output)]).stdout)['frames']
    sizes = [(f['width'], f['height']) for f in frames]
    assert sizes[0] == sizes[-1] == (160, 128)
    assert (192, 128) in sizes and len(sizes) == 270
