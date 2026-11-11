import copy
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest
from test_media_codec import capture as shared_capture

from bilibili_live_monitor import archive_validation as validation
from bilibili_live_monitor import media_codec as codec
from bilibili_live_monitor import recording_archive as archive
from bilibili_live_monitor.archive_media import archive_path, file_stat, receipt_path

capture = shared_capture


def frame(pts, width=1600, height=1280):
    return {'media_type': 'video', 'width': width, 'height': height, 'pix_fmt': 'yuv420p',
            'sample_aspect_ratio': '1:1', 'best_effort_timestamp_time': pts,
            'color_space': 'bt709', 'color_transfer': 'bt709', 'color_primaries': 'bt709'}


def info(frames):
    return {'streams': [{'codec_type': 'video', 'width': 1600, 'height': 1280}],
            'video_evidence': validation.video_evidence(frames)}


def test_mixed_normalization_is_explicit_and_retains_monotonic_frame_times():
    before = info([frame('0'), frame('0', 1920, 1440), frame('0.01')])
    saved = copy.deepcopy(before)
    ref = validation.normalize_reference(before, mixed=True)
    assert before == saved
    assert ref['video_evidence']['shapes'] == [[1920, 1440, 'yuv420p', '1:1']]
    assert validation.unpack(ref['video_evidence']['pts_us']) == [0, 1, 10000]
    assert ref['streams'][0]['display_aspect_ratio'] == '4:3'
    with pytest.raises(ValueError, match='未声明'):
        validation.normalize_reference(before, mixed=False)
    for override in [{'width': 3840}, {'pix_fmt': 'yuv420p10le'}, {'sample_aspect_ratio': '2:1'},
                     {'color_transfer': 'smpte2084'}]:
        with pytest.raises(ValueError, match='未验证'):
            validation.normalize_reference(info([{**frame('0'), **override}]), mixed=True)


def packet(pts, token='A', duration='0.021'):
    return {'pts_time': pts, 'duration_time': duration, 'data_hash': token}


def command_packets(packets):
    return lambda *_: SimpleNamespace(stdout=json.dumps({'packets': packets}), stderr=b'')


def test_audio_retains_first_packet_and_compares_effective_overlapping_spans():
    packets = [packet('0'), packet('0.001', 'B')]
    before = validation.audio_evidence('input', command_packets(packets))
    normalized = validation.audio_evidence('output', command_packets([packet('0', duration='0.001'), packets[1]]))
    assert before == normalized
    assert before['packets'] == 2
    reference = validation.normalize_reference(info([frame('0')]), mixed=False)
    reference['audio_evidence'] = before
    for altered, message in [([packets[1]], '音频包'), ([packet('0', 'different'), packets[1]], '音频包'),
                             ([packets[0], packet('0.02', 'B')], '音频时间轴')]:
        with pytest.raises(ValueError, match=message):
            validation.verify_evidence(reference, reference, 'output', command_packets(altered))
    validation.verify_evidence(reference, reference, 'output', command_packets(packets))


def test_wrong_per_frame_dimensions_and_pts_are_rejected():
    reference = validation.normalize_reference(info([frame('0'), frame('0.033')]), mixed=True)
    reference['audio_evidence'] = validation.audio_evidence('input', command_packets([packet('0')]))
    wrong = info([frame('0'), frame('0.033')])
    with pytest.raises(ValueError, match='逐帧画幅'):
        validation.verify_evidence(reference, wrong, 'output', command_packets([packet('0')]))
    wrong = copy.deepcopy(reference)
    wrong['video_evidence']['pts_us'] = validation.pack([0, 33003])
    with pytest.raises(ValueError, match='视频逐帧时间轴'):
        validation.verify_evidence(reference, wrong, 'output', command_packets([packet('0')]))


def test_real_mixed_archive_retains_all_frames_audio_and_source_identity(capture, tmp_path, monkeypatch):
    source = tmp_path / '01234567-20300101T000000Z-89abcdef-00000.mkv'
    data = (capture / source.name).read_bytes()
    # FFmpeg 6's raw-H264/segment mux path can declare 25fps despite the
    # explicitly generated 30fps packet timeline. Canonicalize this fixture's
    # video DefaultDuration (40ms -> 33.333333ms), without changing any packet.
    old_duration = b'\x23\xe3\x83\x84' + (40_000_000).to_bytes(4, 'big')
    new_duration = b'\x23\xe3\x83\x84' + (33_333_333).to_bytes(4, 'big')
    assert data[:65536].count(old_duration) <= 1
    data = data[:65536].replace(old_duration, new_duration) + data[65536:]
    # Valid EBML Void padding makes savings independent of the tiny test picture.
    source.write_bytes(data + b'\xec' + (0x10000000 | 1048576).to_bytes(4, 'big') + b'\0' * 1048576)
    # The first AAC SimpleBlock is deliberately non-key, as in real segmented
    # recordings. Default FFmpeg stream-copy drops it without -copyinkf:a.
    packets = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_packets',
                              '-show_entries', 'packet=pos', '-of', 'json', str(source)],
                             check=True, capture_output=True).stdout
    pos = int(json.loads(packets)['packets'][0]['pos'])
    raw = bytearray(source.read_bytes())
    assert raw[pos] == 0x82
    raw[pos + 3] &= 0x7f
    source.write_bytes(raw)
    os.utime(source, (time.time() - 60, time.time() - 60))
    original = codec._sha(source)
    workflow = tmp_path / 'workflow.json'
    workflow.write_text('{"stage":"completed"}')
    command = archive.command
    observed = []
    def portable(args, **kwargs):
        if 'hevc_videotoolbox' in args:
            observed.append(args[:])
            args = args[:]
            args[args.index('hevc_videotoolbox')] = 'libx265'
            for flag in ('-allow_sw', '-require_sw'):
                index = args.index(flag)
                del args[index:index + 2]
            args[-1:-1] = ['-preset', 'ultrafast', '-x265-params', 'pools=1:frame-threads=1']
        return command(args, **kwargs)
    monkeypatch.setattr(archive, 'command', portable)
    monkeypatch.setattr(archive, 'no_writer', lambda _: None)
    item = {'source': str(source), 'output': str(archive_path(source)), 'source_stat': file_stat(source),
            'workflow': str(workflow), 'legacy_row': {'sha256': original}}
    profile = archive.probe(source)['streams']
    try:
        archive.check_profile({'streams': profile})
    except ValueError as error:
        pytest.fail(f'合成fixture规格错误：{profile!r}: {error}')
    with archive.writer_lock(tmp_path) as fd:
        assert archive.encode(item, fd)['status'] == 'archived'
    assert codec._sha(source) == original
    saved = json.loads(receipt_path(source).read_text())
    assert saved['codec_parameters']['status'] == 'mixed_parameters'
    assert saved['normalization'] == 'aspect-preserving-1920x1440'
    assert saved['source_info']['video_evidence']['frames'] == saved['pre_normalization_source_info']['video_evidence']['frames']
    assert saved['source_info']['audio_evidence']['packets'] == len(json.loads(packets)['packets'])
    assert '-copyinkf:a' in observed[0] and '-reinit_filter' in observed[0]
    # The stored reference is reusable by the exact cleanup/verify entrypoint.
    archive.verify_output(saved['source_info'], archive_path(source))
