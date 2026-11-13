"""逐帧/逐包验收；保留可在清理原片前重验的紧凑时间轴证据。"""
import base64
import hashlib
import json
import zlib
from fractions import Fraction

LEGACY_POLICY = 'adaptive-pk-aac-v1'
POLICY = 'adaptive-pk-aac-v2'
POLICIES = {LEGACY_POLICY, POLICY}
CANVAS = (1920, 1440)
TIME_FILTER = 'settb=AVTB,setpts=if(eq(N\\,0)\\,PTS\\,max(PTS\\,PREV_OUTPTS+1))'
MIXED_FILTER = ('scale=1920:1440:force_original_aspect_ratio=decrease:force_divisible_by=2:eval=frame,'
                'pad=1920:1440:(ow-iw)/2:(oh-ih)/2:eval=frame,setsar=1,' + TIME_FILTER)


def microseconds(value):
    return round(Fraction(value) * 1_000_000)


def pack(values):
    return base64.b64encode(zlib.compress(json.dumps(values, separators=(',', ':')).encode())).decode()


def unpack(value):
    try:
        return json.loads(zlib.decompress(base64.b64decode(value)))
    except (ValueError, zlib.error) as error:
        raise ValueError('归档验收时间轴无效') from error


def video_evidence(frames):
    frames = [frame for frame in frames if frame['media_type'] == 'video']
    if not frames:
        raise ValueError('没有可验证的视频帧')
    shapes = sorted({(f['width'], f['height'], f['pix_fmt'], f.get('sample_aspect_ratio') or 'unknown') for f in frames})
    return {'frames': len(frames), 'shapes': [list(shape) for shape in shapes],
            'colors': [list(color) for color in sorted({tuple(f.get(k) or 'unknown' for k in
                       ('color_space', 'color_transfer', 'color_primaries')) for f in frames})],
            'pts_us': pack([microseconds(f['best_effort_timestamp_time']) for f in frames])}


def audio_evidence(path, command):
    result = command(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_packets', '-show_frames',
                      '-show_streams', '-show_data_hash', 'sha256', '-show_entries',
                      'packet=data_hash,pts_time,duration_time,pos:'
                      'packet_side_data=side_data_type,skip_samples,discard_padding:'
                      'frame=pkt_pos,nb_samples:'
                      'stream=codec_name,profile,sample_rate,channels,channel_layout,extradata_hash',
                      '-of', 'json', str(path)])
    if result.stderr:
        raise ValueError('音频包读取报错，保留原片')
    data = json.loads(result.stdout)
    entries = data['packets_and_frames']
    packets = [entry for entry in entries if entry['type'] == 'packet']
    if not packets:
        raise ValueError('没有可验证的音频包')
    pts = [microseconds(p['pts_time']) for p in packets]
    # 仅保留给历史v1回执重验；v2不将名义duration当成跨容器有效样本跨度。
    spans = [min(microseconds(p['duration_time']), pts[i + 1] - pts[i])
             if i + 1 < len(packets) else microseconds(p['duration_time']) for i, p in enumerate(packets)]
    digest = hashlib.sha256('\n'.join(p['data_hash'] for p in packets).encode()).hexdigest()
    streams = data['streams']
    if len(streams) != 1:
        raise ValueError('无法验证单路音频配置')
    stream = streams[0]
    config = {key: stream.get(key) for key in ('codec_name', 'profile', 'sample_rate', 'channels',
                                              'channel_layout', 'extradata_hash')}
    if (config['codec_name'] != 'aac' or not all(config[key] for key in
            ('profile', 'sample_rate', 'channels', 'extradata_hash'))):
        raise ValueError('缺少可信AAC解码配置')
    positions = {p['pos']: i for i, p in enumerate(packets)}
    if len(positions) != len(packets) or 'N/A' in positions:
        raise ValueError('音频包位置不能唯一绑定解码帧')
    samples = [0] * len(packets)
    for frame in (entry for entry in entries if entry['type'] == 'frame'):
        if frame.get('pkt_pos') not in positions or frame.get('nb_samples', 0) <= 0:
            raise ValueError('音频解码帧缺少可信样本范围')
        samples[positions[frame['pkt_pos']]] += frame['nb_samples']
    trims = []
    for packet in packets:
        skip = discard = 0
        for side in packet.get('side_data_list', []):
            if side.get('side_data_type') == 'Skip Samples':
                skip += side.get('skip_samples', 0)
                discard += side.get('discard_padding', 0)
        trims.append([skip, discard])
    return {'packets': len(packets), 'payload_sha256': digest, 'pts_us': pack(pts), 'spans_us': pack(spans),
            'config': config, 'decoded_samples': pack(samples), 'trims': pack(trims)}


def audio_tail_filter(evidence):
    """Only extend an untrimmed last packet to contain its actual decoded samples.

    MKV's millisecond nominal duration can otherwise make MP4 discard valid AAC
    tail samples. PTS/DTS and every interior packet remain unchanged. Existing
    legitimate trim is never cleared or guessed; strict sample validation follows.
    """
    samples, trims = unpack(evidence['decoded_samples']), unpack(evidence['trims'])
    if not samples or samples[-1] <= 0 or trims[-1] != [0, 0]:
        return []
    rate = int(evidence['config']['sample_rate'])
    if rate <= 0:
        raise ValueError('音频采样率无效')
    expression = (f'setts=duration=if(eq(N\\,{evidence["packets"] - 1})\\,'
                  f'max(DURATION\\,ceil({samples[-1]}/{rate}/TB))\\,DURATION)')
    return ['-bsf:a', expression]


def normalize_reference(info, *, mixed):
    """只改变预期解码画布；真实输入元数据另存于归档回执。"""
    reference = json.loads(json.dumps(info))
    video = next(s for s in reference['streams'] if s['codec_type'] == 'video')
    evidence = reference['video_evidence']
    if evidence['colors'] != [['bt709', 'bt709', 'bt709']]:
        raise ValueError('未验证的逐帧色彩参数；保留原片')
    for width, height, pixel, sar in evidence['shapes']:
        if (not 0 < width <= 1920 or not 0 < height <= 1440 or width % 2 or height % 2
                or pixel != 'yuv420p' or sar != '1:1'):
            raise ValueError('未验证的逐帧画幅、像素格式或像素比例；保留原片')
    if not mixed and len(evidence['shapes']) != 1:
        raise ValueError('发现未声明的片内画幅变化；保留原片')
    if mixed:
        video.update(width=CANVAS[0], height=CANVAS[1], coded_width=CANVAS[0], coded_height=CANVAS[1],
                     sample_aspect_ratio='1:1', display_aspect_ratio='4:3')
        evidence['shapes'] = [[*CANVAS, 'yuv420p', '1:1']]
    else:
        # Stream metadata may describe an old container epoch. The actual
        # decoded frames, rather than that stale display size, define native.
        width, height, _, _ = evidence['shapes'][0]
        video.update(width=width, height=height, sample_aspect_ratio='1:1')
    video['nb_read_frames'] = str(evidence['frames'])
    pts, normalized = unpack(evidence['pts_us']), []
    for value in pts:
        normalized.append(max(value, normalized[-1] + 1) if normalized else value)
    evidence['pts_us'] = pack(normalized)
    reference['validation_policy'] = POLICY
    return reference


def verify_evidence(reference, output, target, command):
    before, after = reference['video_evidence'], output['video_evidence']
    if (before['shapes'] != after['shapes'] or before['frames'] != after['frames']
            or before['colors'] != after['colors']):
        raise ValueError('逐帧画幅、比例或帧数改变，保留原片')
    def close(a, b, tolerance):
        a, b = unpack(a), unpack(b)
        return len(a) == len(b) and all(abs(x - y) <= tolerance for x, y in zip(a, b))
    if not close(before['pts_us'], after['pts_us'], 2):
        raise ValueError('视频逐帧时间轴改变，保留原片')
    before, after = reference['audio_evidence'], audio_evidence(target, command)
    if before['packets'] != after['packets'] or before['payload_sha256'] != after['payload_sha256']:
        raise ValueError('音频包丢失或内容改变，保留原片')
    if not close(before['pts_us'], after['pts_us'], 1000):
        raise ValueError('音频时间轴改变，保留原片')
    if reference.get('validation_policy') == LEGACY_POLICY:
        if not close(before['spans_us'], after['spans_us'], 1000):
            raise ValueError('音频时间轴改变，保留原片')
        return  # Old receipts cannot claim evidence they never recorded.
    if before['config'] != after['config']:
        raise ValueError('音频解码配置改变，保留原片')
    if unpack(before['trims']) != unpack(after['trims']):
        raise ValueError('音频裁剪样本改变，保留原片')
    if unpack(before['decoded_samples']) != unpack(after['decoded_samples']):
        raise ValueError('音频有效样本改变，保留原片')
