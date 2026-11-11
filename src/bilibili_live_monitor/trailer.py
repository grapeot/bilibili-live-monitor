"""从已审阅的本地精剪表生成预告片；不自动把互动峰值当精彩内容。"""
import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path


def configure_parser(commands):
    command = commands.add_parser('trailer', help='按已审阅精剪表生成本地预告片')
    actions = command.add_subparsers(dest='action', required=True)
    render = actions.add_parser('render')
    render.add_argument('--plan', type=Path, required=True)
    render.add_argument('--subject', required=True)
    render.add_argument('--font', type=Path, required=True, help='本地支持中文的TTF字体')
    render.add_argument('--output', type=Path, required=True, help='新的私有输出目录')
    render.add_argument('--allow-dynamic-normalization', action='store_true',
                        help='显式接受峰值受限时的动态响度处理；仍须试听')
    check = actions.add_parser('verify')
    check.add_argument('--output', type=Path, required=True)


def run(command, cwd=None):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f'{command[0]} exit {result.returncode}: {result.stderr}')
    return result


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                           '-of', 'json', str(path)]).stdout)


def coverage(path, info):
    """使用实际包时间而非容器总长，拒绝缺轨和采集空洞。"""
    packets = json.loads(run(['ffprobe', '-v', 'error', '-show_packets', '-show_entries',
                              'packet=stream_index,pts_time,duration_time', '-of', 'json', str(path)]).stdout)
    origin = float(info['format'].get('start_time', 0))
    result = {}
    for kind in ('video', 'audio'):
        stream = next(s for s in info['streams'] if s['codec_type'] == kind)
        spans = []
        for packet in packets['packets']:
            if packet['stream_index'] != stream['index'] or 'pts_time' not in packet:
                continue
            start = float(packet['pts_time']) - origin
            length = float(packet.get('duration_time', 0))
            if math.isfinite(start) and math.isfinite(length) and length > 0:
                spans.append((start, start + length))
        if not spans:
            raise ValueError(f'{kind}没有有效包时间，无法确认覆盖')
        result[kind] = sorted(spans)
    return result


def require_coverage(spans, start, end, tolerance=.12):
    for kind, ranges in spans.items():
        cursor = start
        for left, right in ranges:
            if right <= cursor:
                continue
            if left > cursor + tolerance:
                raise ValueError(f'{kind}存在覆盖缺口：{cursor:.3f}至{left:.3f}')
            cursor = max(cursor, right)
            if cursor >= end - tolerance:
                break
        if cursor < end - tolerance:
            raise ValueError(f'{kind}提前结束：{cursor:.3f}，需要到{end:.3f}')


def measure_audio(path):
    measured = run(['ffmpeg', '-v', 'info', '-nostdin', '-threads', '2', '-i', str(path), '-vn',
                    '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json', '-f', 'null', '-'])
    return parse_loudness(measured.stderr)


def parse_loudness(stderr):
    # ffmpeg的新版本会在测量JSON之后继续打印收尾统计。
    start = stderr.rfind('{')
    if start < 0:
        raise ValueError(f'未取得响度JSON：{stderr}')
    value, _ = json.JSONDecoder().raw_decode(stderr[start:])
    if not isinstance(value, dict) or 'input_i' not in value:
        raise ValueError('无效的响度测量对象')
    return value


def check_normalization(mode, accepted=False):
    if mode == 'linear' or mode == 'dynamic' and accepted is True:
        return
    raise ValueError('响度发生动态归一回退，需显式接受并试听，或调整音频方案')


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('时间必须是有限数字')
    return value


def rectangle(value, width, height):
    if not isinstance(value, list) or len(value) != 4 or any(type(n) is not int for n in value):
        raise ValueError('矩形必须为四个整数[x,y,w,h]')
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height:
        raise ValueError('矩形超出源画面')
    return value


def validate(plan, subject):
    from .archive_media import resolve_media
    if plan.get('schema_version') != 1:
        raise ValueError('不支持的精剪表版本')
    selected = plan['subjects'][subject]
    clips = selected['clips']
    if not isinstance(clips, list) or not 1 <= len(clips) <= 40:
        raise ValueError('需要1至40个片段')
    sources, normalized = {}, []
    for clip in clips:
        original = Path(clip['source']).resolve()
        source = resolve_media(original, expected_sha=clip['sha256'], video=True)
        if not source.is_file():
            raise ValueError('源必须是本地文件')
        if source not in sources:
            info = probe(source)
            video = next(s for s in info['streams'] if s['codec_type'] == 'video')
            if not any(s['codec_type'] == 'audio' for s in info['streams']):
                raise ValueError('源片缺少音轨')
            if any(s.get('side_data_type') == 'Display Matrix' for s in video.get('side_data_list', [])):
                raise ValueError('不支持带显示变换矩阵的源片，先在私有流程明确归一方向与坐标')
            sources[source] = (digest(source) if source == original else None,
                               video, float(info['format']['duration']), coverage(source, info))
        sha, video, duration, spans = sources[source]
        if sha is not None and clip['sha256'] != sha:
            raise ValueError('源片指纹不符')
        start, end = number(clip['start']), number(clip['end'])
        if not 0 <= start < end <= duration:
            raise ValueError('片段时间超出源文件')
        require_coverage(spans, start, end)
        crop = rectangle(clip['crop'], video['width'], video['height'])
        masks = [rectangle(mask, video['width'], video['height']) for mask in clip.get('masks', [])]
        label = clip['label']
        if not isinstance(label, str) or not 1 <= len(label) <= 36 or any(c in label for c in '\r\n\x00'):
            raise ValueError('情境标题须为1至36字符的单行文字')
        normalized.append({**clip, 'source': str(source), 'crop': crop, 'masks': masks,
                           **({'archived_from': str(original)} if source != original else {})})
    duration = sum(c['end'] - c['start'] for c in normalized)
    if not 120 <= duration <= 180:
        raise ValueError('预告片必须为120至180秒，不能靠截断句子凑时长')
    return {**selected, 'clips': normalized, 'duration': duration}


def video_filter(clip):
    filters = [f'drawbox=x={x}:y={y}:w={w}:h={h}:color=black:t=fill' for x, y, w, h in clip['masks']]
    x, y, w, h = clip['crop']
    filters.extend([f'crop={w}:{h}:{x}:{y}', 'scale=1280:640:force_original_aspect_ratio=decrease',
                    'pad=1280:720:(ow-iw)/2:(640-ih)/2:color=black', 'setsar=1', 'fps=30'])
    return ','.join(filters)


def render_label(text, font, path):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new('RGBA', (1280, 720))
    draw = ImageDraw.Draw(image)
    face = ImageFont.truetype(str(font), 30)
    draw.text((640, 680), text, font=face, fill='white', anchor='mm')
    image.save(path)


def verify(output):
    manifest = json.loads((output / 'manifest.json').read_text())
    if manifest.get('status') != 'complete':
        raise ValueError('渲染流程尚未完整完成')
    movie = output / 'trailer.mp4'
    if digest(movie) != manifest['sha256']:
        raise ValueError('成片指纹不符')
    info = probe(movie)
    duration = float(info['format']['duration'])
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    audio = next(s for s in info['streams'] if s['codec_type'] == 'audio')
    if abs(duration - manifest['duration']) > .25 or not 120 <= duration <= 180.25:
        raise ValueError('成片时长不符')
    if (video['width'], video['height'], video['codec_name'], audio['codec_name']) != (1280, 720, 'h264', 'aac'):
        raise ValueError('成片格式不符')
    require_coverage(coverage(movie, info), 0, manifest['duration'])
    run(['ffmpeg', '-v', 'error', '-xerror', '-threads', '2', '-i', str(movie), '-f', 'null', '-'])
    loudness = json.loads((output / 'loudness.json').read_text())
    if loudness['sha256'] != manifest['sha256']:
        raise ValueError('响度证据不匹配')
    mode = loudness['normalized']['normalization_type']
    check_normalization(mode, manifest.get('dynamic_normalization_accepted', False))
    measured = measure_audio(movie)
    level, peak = float(measured['input_i']), float(measured['input_tp'])
    if not math.isfinite(level) or not math.isfinite(peak) or abs(level + 16) > 1 or peak > -1:
        raise ValueError(f'最终AAC响度超出容差：{level} LUFS，{peak} dBTP')
    return {'verified': True, 'duration': duration, 'clips': len(manifest['clips']),
            'sha256': manifest['sha256'], 'output': str(movie), 'human_listening_required': True,
            'integrated_lufs': level, 'true_peak_dbtp': peak, 'normalization_type': mode}


def render(plan, subject, font, output, allow_dynamic=False):
    if output.exists():
        raise ValueError('输出目录已存在，请保留旧版并使用新目录')
    selected = validate(plan, subject)
    if not font.is_file():
        raise ValueError('找不到本地字体')
    output.mkdir(parents=True)
    output = output.resolve()
    shutil.copyfile(font, output / 'font.ttf')
    (output / 'plan.json').write_text(json.dumps(selected, ensure_ascii=False, indent=2))
    for index, clip in enumerate(selected['clips']):
        label = f'label_{index:02}.png'
        render_label(clip['label'], output / 'font.ttf', output / label)
        result = run(['ffmpeg', '-v', 'warning', '-nostdin', '-threads', '2', '-noautorotate', '-i', clip['source'],
                      '-loop', '1', '-framerate', '30', '-i', label, '-filter_complex_threads', '2',
                      '-ss', str(clip['start']), '-t', str(clip['end'] - clip['start']),
                      '-map', '[v]', '-map', '0:a:0', '-filter_complex',
                      f'[0:v]{video_filter(clip)}[base];[base][1:v]overlay=0:0:shortest=1[v]',
                      '-af', 'aresample=48000:async=1:first_pts=0,asetpts=PTS-STARTPTS',
                      '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p',
                      '-threads', '2', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2',
                      '-map_metadata', '-1', f'part_{index:02}.mp4'], cwd=output)
        (output / f'part_{index:02}.log').write_text(result.stderr)
        if any(marker in result.stderr.lower() for marker in ('error while decoding', 'corrupt decoded frame', 'invalid nal')):
            raise ValueError(f'片段{index}存在源解码错误，先检查私有part日志并改选素材')
        part = output / f'part_{index:02}.mp4'
        require_coverage(coverage(part, probe(part)), 0, clip['end'] - clip['start'])
    (output / 'concat.txt').write_text(''.join(f"file 'part_{i:02}.mp4'\n" for i in range(len(selected['clips']))))
    run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'concat', '-safe', '1', '-i', 'concat.txt',
         '-c', 'copy', 'joined.mp4'], cwd=output)
    loudness = measure_audio(output / 'joined.mp4')
    if not all(math.isfinite(float(loudness[k])) for k in ('input_i', 'input_tp', 'input_lra', 'input_thresh')):
        raise ValueError('无法测量有效原声响度')
    lra = max(11, math.ceil(float(loudness['input_lra'])))
    normalization = (f'loudnorm=I=-16:TP=-1.5:LRA={lra}:linear=true:print_format=json:'
                     f'measured_I={loudness["input_i"]}:measured_TP={loudness["input_tp"]}:'
                     f'measured_LRA={loudness["input_lra"]}:measured_thresh={loudness["input_thresh"]}:'
                     f'offset={loudness["target_offset"]}')
    result = run(['ffmpeg', '-v', 'info', '-nostdin', '-i', 'joined.mp4', '-c:v', 'copy',
                  '-af', normalization, '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
                  '-map_metadata', '-1', '-movflags', '+faststart', 'trailer.mp4'], cwd=output)
    measured = parse_loudness(result.stderr)
    check_normalization(measured['normalization_type'], allow_dynamic)
    (output / 'loudness.json').write_text(json.dumps({'input': loudness, 'normalized': measured,
                                                    'sha256': digest(output / 'trailer.mp4')}, indent=2))
    manifest = {**selected, 'sha256': digest(output / 'trailer.mp4'),
                'human_listening_required': True, 'visual_privacy_review_required': True,
                'dynamic_normalization_accepted': allow_dynamic is True}
    cursor = 0.0
    for index, clip in enumerate(selected['clips']):
        duration = clip['end'] - clip['start']
        for edge, offset in [('in', .1), ('out', max(.1, duration - .1))]:
            run(['ffmpeg', '-v', 'error', '-threads', '2', '-ss', str(cursor + offset),
                 '-i', 'trailer.mp4', '-frames:v', '1', f'edge_{index:02}_{edge}.jpg'], cwd=output)
        cursor += float(probe(output / f'part_{index:02}.mp4')['format']['duration'])
    step = selected['duration'] / 12
    run(['ffmpeg', '-v', 'error', '-threads', '2', '-i', 'trailer.mp4',
         '-vf', f'fps=1/{step},scale=320:180,tile=4x3', '-frames:v', '1', 'contact.jpg'], cwd=output)
    manifest['status'] = 'complete'
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return verify(output)


def main(args):
    try:
        result = (verify(args.output.resolve()) if args.action == 'verify' else
                  render(json.loads(args.plan.read_text()), args.subject, args.font, args.output,
                         args.allow_dynamic_normalization))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, StopIteration) as error:
        print(json.dumps({'error': f'{type(error).__name__}: {error}'}, ensure_ascii=False), file=sys.stderr)
        return 2
