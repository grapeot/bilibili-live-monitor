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


def command_packets(packets, **config):
    entries = []
    for i, packet in enumerate(packets):
        entries.append({'type': 'packet', 'pos': str(i), **packet})
        if packet.get('decoded_samples', 1024):
            entries.append({'type': 'frame', 'pkt_pos': str(i), 'nb_samples': packet.get('decoded_samples', 1024)})
    stream = {'codec_name': 'aac', 'profile': 'LC', 'sample_rate': '48000', 'channels': 2,
              'channel_layout': 'stereo', 'extradata_hash': 'SHA256:synthetic', **config}
    return lambda *_: SimpleNamespace(stdout=json.dumps({'packets_and_frames': entries, 'streams': [stream]}), stderr=b'')


def test_audio_retains_first_packet_and_preserves_overlapping_packet_anchors():
    packets = [packet('0'), packet('0.001', 'B')]
    before = validation.audio_evidence('input', command_packets(packets))
    normalized = validation.audio_evidence('output', command_packets([packet('0', duration='0.001'), packets[1]]))
    assert before == normalized
    assert before['packets'] == 2
    reference = validation.normalize_reference(info([frame('0')]), mixed=False)
    reference['audio_evidence'] = before
    for altered, message in [([packets[1]], '音频包'), (list(reversed(packets)), '音频包'),
                             ([packet('0', 'different'), packets[1]], '音频包'),
                             ([packet('0.002'), packet('0.003', 'B')], '音频时间轴'),
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


def test_aac_gap_uses_packet_anchors_not_mkv_nominal_duration():
    # Same AAC packets and preserved presentation gap: MKV reports nominal AAC
    # duration, MP4 stts records the interval through the gap to the next packet.
    source = [packet('1.000'), packet('1.023', 'B')]
    output = [packet('1.000167', duration='0.022333'), packet('1.022500', 'B')]
    reference = validation.normalize_reference(info([frame('0')]), mixed=False)
    reference['audio_evidence'] = validation.audio_evidence('source', command_packets(source))
    validation.verify_evidence(reference, reference, 'output', command_packets(output))


def test_aac_adjacent_anchor_rounding_does_not_require_interval_error_below_one_ms():
    source = [packet('1.000'), packet('1.023', 'B')]
    output = [packet('1.000800'), packet('1.022200', 'B')]
    reference = validation.normalize_reference(info([frame('0')]), mixed=False)
    reference['audio_evidence'] = validation.audio_evidence('source', command_packets(source))
    validation.verify_evidence(reference, reference, 'output', command_packets(output))


@pytest.mark.parametrize('alteration,message', [
    ({'side_data_list': [{'side_data_type': 'Skip Samples', 'discard_padding': 16}],
      'decoded_samples': 1008}, '裁剪样本'),
    ({'side_data_list': [{'side_data_type': 'Skip Samples', 'skip_samples': 16}],
      'decoded_samples': 1008}, '裁剪样本'),
    ({'decoded_samples': 1008}, '有效样本'),
    ({'pts_time': '1.002'}, '时间轴'),
])
@pytest.mark.parametrize('index', [0, 1, 2])
def test_aac_sample_loss_or_anchor_shift_is_rejected(alteration, message, index):
    source = [packet('1.000'), packet('1.021', 'B'), packet('1.043', 'C')]
    output = copy.deepcopy(source)
    output[index].update(alteration)
    reference = validation.normalize_reference(info([frame('0')]), mixed=False)
    reference['audio_evidence'] = validation.audio_evidence('source', command_packets(source))
    with pytest.raises(ValueError, match=message):
        validation.verify_evidence(reference, reference, 'output', command_packets(output))


@pytest.mark.parametrize('config', [{'sample_rate': '44100'}, {'profile': 'HE-AAC'}, {'channels': 1},
                                    {'channel_layout': 'mono'}, {'extradata_hash': 'SHA256:changed'}])
def test_aac_decoder_config_change_is_rejected(config):
    packets = [packet('0')]
    reference = validation.normalize_reference(info([frame('0')]), mixed=False)
    reference['audio_evidence'] = validation.audio_evidence('source', command_packets(packets))
    with pytest.raises(ValueError, match='解码配置'):
        validation.verify_evidence(reference, reference, 'output', command_packets(packets, **config))


def test_aac_legitimate_padding_is_preserved_and_not_overridden():
    packets = [{**packet('0'), 'decoded_samples': 1008,
                'side_data_list': [{'side_data_type': 'Skip Samples', 'discard_padding': 16}]}]
    reference = validation.normalize_reference(info([frame('0')]), mixed=False)
    reference['audio_evidence'] = validation.audio_evidence('source', command_packets(packets))
    validation.verify_evidence(reference, reference, 'output', command_packets(packets))
    assert validation.audio_tail_filter(reference['audio_evidence']) == []


def test_aac_tail_filter_uses_actual_samples_and_rate_without_retiming():
    evidence = validation.audio_evidence('source', command_packets([
        packet('0'), {**packet('0.043'), 'decoded_samples': 2048}], sample_rate='44100'))
    assert validation.audio_tail_filter(evidence) == [
        '-bsf:a', 'setts=duration=if(eq(N\\,1)\\,max(DURATION\\,ceil(2048/44100/TB))\\,DURATION)']


def test_v1_receipt_retains_its_original_checks_without_claiming_sample_evidence():
    packets = [packet('0')]
    reference = validation.normalize_reference(info([frame('0')]), mixed=False)
    reference['validation_policy'] = validation.LEGACY_POLICY
    evidence = validation.audio_evidence('source', command_packets(packets))
    reference['audio_evidence'] = {k: v for k, v in evidence.items() if k not in ('config', 'decoded_samples', 'trims')}
    validation.verify_evidence(reference, reference, 'output', command_packets(packets))
    with pytest.raises(ValueError, match='时间轴'):
        validation.verify_evidence(reference, reference, 'output', command_packets([packet('0', duration='0.019')]))


def shift_fixture_audio_blocks(data, milliseconds):
    """仅构造测试输入：按EBML结构移动全部音频Block，保留payload及视频时序。"""
    patched = bytearray(data)
    masters = {0x18538067, 0x1654ae6b, 0xae, 0x1f43b675, 0xa0}
    elements, audio_tracks = [], set()
    def walk(start, end):
        for begin, tag, payload, finish in codec._elements(data, start, end):
            elements.append((begin, tag, payload, finish))
            if tag == 0xae:
                fields = {tag: data[payload:finish] for _, tag, payload, finish in codec._elements(data, payload, finish)}
                if int.from_bytes(fields[0x83], 'big') == 2:
                    audio_tracks.add(int.from_bytes(fields[0xd7], 'big'))
            if tag in masters:
                walk(payload, finish)
    walk(0, 1 << 63)
    assert len(audio_tracks) == 1
    blocks = 0
    for begin, tag, payload, finish in elements:
        if tag == 0xbf:
            assert finish - payload == 4
            patched[begin] = 0xec  # 更新后的祖先CRC变成等长Void。
        elif tag in (0xa3, 0xa1):
            width = 9 - data[payload].bit_length()
            track = int.from_bytes(data[payload:payload + width], 'big') & ((1 << (7 * width)) - 1)
            if track in audio_tracks:
                offset = payload + width
                value = int.from_bytes(data[offset:offset + 2], 'big', signed=True) - milliseconds
                assert -32768 <= value <= 32767
                patched[offset:offset + 2] = value.to_bytes(2, 'big', signed=True)
                blocks += 1
    assert blocks > 0
    return bytes(patched)


@pytest.mark.parametrize('audio_boundary', ['zero_start', 'nonzero_start', 'initial_skip'])
def test_real_mixed_archive_retains_all_frames_audio_and_source_identity(capture, tmp_path, monkeypatch, audio_boundary):
    source = tmp_path / '01234567-20300101T000000Z-89abcdef-00000.mkv'
    data = (capture / source.name).read_bytes()
    # FFmpeg 6's raw-H264/segment mux path can declare 25fps despite the
    # explicitly generated 30fps packet timeline. Canonicalize this fixture's
    # video DefaultDuration (40ms -> 33.333333ms), without changing any packet.
    old_duration = b'\x23\xe3\x83\x84' + (40_000_000).to_bytes(4, 'big')
    new_duration = b'\x23\xe3\x83\x84' + (33_333_333).to_bytes(4, 'big')
    assert data[:65536].count(old_duration) <= 1
    data = data[:65536].replace(old_duration, new_duration) + data[65536:]
    if audio_boundary != 'initial_skip':
        # Model segmented captures without encoder priming. Keep the original
        # primed variant too: MP4 currently loses its initial Skip Samples, and
        # the stricter contract must reject it rather than publish changed PCM.
        delay = b'\x56\xaa\x84' + (21_333_333).to_bytes(4, 'big')
        assert data[:65536].count(delay) == 1
        data = data[:65536].replace(delay, b'\x56\xaa\x84' + b'\0' * 4) + data[65536:]
    data = shift_fixture_audio_blocks(data, 21 if audio_boundary == 'zero_start' else 0)
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
    source_audio = validation.audio_evidence(source, archive.command)
    if audio_boundary == 'initial_skip':
        # Older Matroska demuxers do not expose CodecDelay as packet skip data.
        # Keep the declared priming input and test the actual decoder contract.
        assert validation.unpack(source_audio['trims'])[0] in ([1024, 0], [0, 0])
    else:
        assert all(trim == [0, 0] for trim in validation.unpack(source_audio['trims']))
        assert all(count == 1024 for count in validation.unpack(source_audio['decoded_samples']))
        assert validation.unpack(source_audio['pts_us'])[0] == (0 if audio_boundary == 'zero_start' else 21000)
    # Input construction must retain every AAC payload and the video timeline.
    before_audio = validation.audio_evidence(capture / source.name, archive.command)
    assert source_audio['payload_sha256'] == before_audio['payload_sha256']
    assert source_audio['packets'] == before_audio['packets']
    def video_packets(path):
        return json.loads(archive.command(['ffprobe', '-v', 'fatal', '-select_streams', 'v:0', '-show_packets',
                                          '-show_data_hash', 'sha256', '-show_entries',
                                          'packet=pts_time,dts_time,data_hash', '-of', 'json', str(path)]).stdout)
    assert video_packets(source) == video_packets(capture / source.name)
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
    verify_output = archive.verify_output
    observed_audio = []
    def checked_output(reference, path):
        observed_audio.append(validation.audio_evidence(path, portable))
        return verify_output(reference, path)
    monkeypatch.setattr(archive, 'verify_output', checked_output)
    item = {'source': str(source), 'output': str(archive_path(source)), 'source_stat': file_stat(source),
            'workflow': str(workflow), 'legacy_row': {'sha256': original}}
    profile = archive.probe(source)['streams']
    try:
        archive.check_profile({'streams': profile})
    except ValueError as error:
        pytest.fail(f'合成fixture规格错误：{profile!r}: {error}')
    with archive.writer_lock(tmp_path) as fd:
        try:
            result = archive.encode(item, fd)
        except ValueError as error:
            assert audio_boundary != 'zero_start'
            assert '裁剪样本' in str(error) or '有效样本' in str(error)
            assert len(observed_audio) == 1
            after = observed_audio[0]
            assert (validation.unpack(source_audio['trims']) != validation.unpack(after['trims'])
                    or validation.unpack(source_audio['decoded_samples']) != validation.unpack(after['decoded_samples']))
            assert codec._sha(source) == original
            assert not archive_path(source).exists() and not receipt_path(source).exists()
            return
        # A muxer that preserves these boundaries may succeed, but only the
        # strict evidence checks plus complete PCM equality below permit that.
        assert result['status'] == 'archived'
    assert codec._sha(source) == original
    saved = json.loads(receipt_path(source).read_text())
    assert saved['codec_parameters']['status'] == 'mixed_parameters'
    assert saved['normalization'] == 'aspect-preserving-1920x1440'
    assert saved['source_info']['video_evidence']['frames'] == saved['pre_normalization_source_info']['video_evidence']['frames']
    assert saved['source_info']['audio_evidence']['packets'] == len(json.loads(packets)['packets'])
    assert '-copyinkf:a' in observed[0] and '-reinit_filter' in observed[0]
    assert saved['source_info']['validation_policy'] == validation.POLICY
    def pcm_hash(path):
        return command(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-map', '0:a:0',
                        '-c:a', 'pcm_f32le', '-f', 'hash', '-hash', 'sha256', '-']).stdout
    assert pcm_hash(source) == pcm_hash(archive_path(source))
    # The stored reference is reusable by the exact cleanup/verify entrypoint.
    archive.verify_output(saved['source_info'], archive_path(source))
