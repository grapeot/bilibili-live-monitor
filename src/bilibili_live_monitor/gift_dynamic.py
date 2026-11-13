"""限已知舰长昵称文字槽的VAP动态替换；图片、未知语义及样式不降级播放。"""
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

USERNAME_TAG = 'live.guard.open.animation.user.name'


def rect(value, width, height):
    if (not isinstance(value, list) or len(value) != 4 or any(type(n) is not int for n in value)
            or min(value[:2]) < 0 or min(value[2:]) <= 0
            or value[0] + value[2] > width or value[1] + value[3] > height):
        raise ValueError('动态替换区域越界或无效')
    return value


def validate_dynamic(config):
    sources, frames, info = config.get('src') or [], config.get('frame') or [], config['info']
    if not sources and not frames and not info.get('custom'):
        return []
    if not isinstance(sources, list) or not 1 <= len(sources) <= 16 or not isinstance(frames, list):
        raise ValueError('暂不支持此动态替换结构')
    identifiers = set()
    for source in sources:
        if (not isinstance(source, dict) or source.get('srcTag') != USERNAME_TAG
                or source.get('srcType') != 'txt' or source.get('fitType') != 'fitXY'
                or source.get('style') not in (None, '')
                or not isinstance(source.get('color'), str)
                or not re.fullmatch(r'#[0-9a-fA-F]{6}', source['color'])):
            raise ValueError('暂不支持此动态替换语义、类型或文字样式')
        ident = source.get('srcId')
        if not isinstance(ident, str) or not ident or ident in identifiers:
            raise ValueError('动态文字槽ID无效或重复')
        identifiers.add(ident)
        rect([0, 0, source.get('w'), source.get('h')], info['w'], info['h'])
    indices = set()
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError('动态帧无效')
        index, objects = frame.get('i'), frame.get('obj')
        if type(index) is not int or not 0 <= index < info['f'] or index in indices:
            raise ValueError('动态帧编号无效或重复')
        indices.add(index)
        if not isinstance(objects, list) or len(objects) > 16:
            raise ValueError('动态帧对象无效')
        for obj in objects:
            if (not isinstance(obj, dict) or obj.get('srcId') not in identifiers
                    or type(obj.get('z')) is not int or not 0 <= obj['z'] <= 10000):
                raise ValueError('动态帧引用或图层无效')
            rect(obj.get('frame'), info['w'], info['h'])
            rect(obj.get('mFrame'), info['videoW'], info['videoH'])
    return sources


def text_images(config, text, font_path):
    if not isinstance(text, str) or not text.strip() or len(text) > 200 or any(ord(c) < 32 for c in text):
        raise ValueError('动态昵称必须为非空且不超过200字的单行文字')
    result = {}
    for source in validate_dynamic(config):
        width, height = source['w'], source['h']
        image = Image.new('RGBA', (width, height))
        draw = ImageDraw.Draw(image)
        for size in range(max(1, int(height * .75)), 0, -1):
            font = ImageFont.truetype(str(font_path), size)
            box = draw.textbbox((0, 0), text, font=font)
            if box[2]-box[0] <= width and box[3]-box[1] <= height:
                break
        else:
            raise ValueError('昵称无法放入文字槽')
        draw.text(((width-(box[2]-box[0]))/2-box[0], (height-(box[3]-box[1]))/2-box[1]),
                  text, font=font, fill=source['color'])
        result[source['srcId']] = image
    return result


def read_exact(pipe, size):
    result = bytearray()
    while len(result) < size:
        chunk = pipe.read(size-len(result))
        if not chunk:
            break
        result.extend(chunk)
    if len(result) != size:
        raise ValueError('动态动画实际帧数少于配置')
    return bytes(result)


def render_rgba(media, config, text, font_path, output):
    """保留素材帧率；每帧合成图片遮罩后输出带alpha的无损中间视频。"""
    info = config['info']
    images = text_images(config, text, font_path)
    objects = {frame['i']: frame['obj'] for frame in config.get('frame', [])}
    output = Path(output)
    with output.with_suffix('.decode.log').open('wb') as dec_log, output.with_suffix('.encode.log').open('wb') as enc_log:
        decoder = subprocess.Popen(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(media),
            '-map', '0:v:0', '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=dec_log)
        encoder = None
        try:
            encoder = subprocess.Popen(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-f', 'rawvideo',
                '-pixel_format', 'rgba', '-video_size', f'{info["w"]}x{info["h"]}', '-framerate', str(info['fps']),
                '-i', 'pipe:0', '-c:v', 'qtrle', '-pix_fmt', 'argb', str(output)], stdin=subprocess.PIPE, stderr=enc_log)
            for index in range(info['f']):
                packed = Image.frombytes('RGB', (info['videoW'], info['videoH']),
                                         read_exact(decoder.stdout, info['videoW']*info['videoH']*3))

                def crop(rectangle):
                    x, y, w, h = rectangle
                    return packed.crop((x, y, x+w, y+h))

                frame = crop(info['rgbFrame']).convert('RGBA')
                frame.putalpha(crop(info['aFrame']).convert('L').resize(frame.size, Image.Resampling.BILINEAR))
                for obj in sorted(objects.get(index, []), key=lambda item: item['z']):
                    x, y, w, h = obj['frame']
                    replacement = images[obj['srcId']].resize((w, h), Image.Resampling.LANCZOS)
                    mask = crop(obj['mFrame']).convert('L').resize((w, h), Image.Resampling.BILINEAR)
                    replacement.putalpha(ImageChops.multiply(replacement.getchannel('A'), mask))
                    frame.alpha_composite(replacement, (x, y))
                encoder.stdin.write(frame.tobytes())
            if decoder.stdout.read(1):
                raise ValueError('动态动画实际帧数多于配置')
            if decoder.wait(timeout=30):
                raise ValueError('动态动画解码失败，详见decode.log')
            encoder.stdin.close()
            if encoder.wait(timeout=60):
                raise ValueError('动态动画编码失败，详见encode.log')
        finally:
            decoder.stdout.close()
            for proc in (decoder, encoder):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait()
            if encoder is not None and encoder.stdin and not encoder.stdin.closed:
                try:
                    encoder.stdin.close()
                except BrokenPipeError:
                    pass
    return output
