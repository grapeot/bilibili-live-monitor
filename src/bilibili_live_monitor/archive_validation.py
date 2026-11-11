"""逐帧/逐包验收；保留可在清理原片前重验的紧凑时间轴证据。"""
import base64
import hashlib
import json
import zlib
from fractions import Fraction

POLICY = 'adaptive-pk-aac-v1'
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
    result = command(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_packets',
                      '-show_data_hash', 'sha256', '-show_entries', 'packet=data_hash,pts_time,duration_time',
                      '-of', 'json', str(path)])
    if result.stderr:
        raise ValueError('音频包读取报错，保留原片')
    packets = json.loads(result.stdout)['packets']
    if not packets:
        raise ValueError('没有可验证的音频包')
    pts = [microseconds(p['pts_time']) for p in packets]
    spans = [min(microseconds(p['duration_time']), pts[i + 1] - pts[i])
             if i + 1 < len(packets) else microseconds(p['duration_time']) for i, p in enumerate(packets)]
    digest = hashlib.sha256('\n'.join(p['data_hash'] for p in packets).encode()).hexdigest()
    return {'packets': len(packets), 'payload_sha256': digest, 'pts_us': pack(pts), 'spans_us': pack(spans)}


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
    if (not close(before['pts_us'], after['pts_us'], 1000)
            or not close(before['spans_us'], after['spans_us'], 1000)):
        raise ValueError('音频时间轴改变，保留原片')
