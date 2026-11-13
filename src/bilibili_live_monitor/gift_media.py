"""离线礼物切片的只读来源定位、时间映射和画幅检查。"""
import json
import math
import shutil
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from .archive_media import archive_receipt, archived_input, file_stat, receipt_path
from .gift_assets import sha_file, write_json
from .media_codec import CAPTURE, _check_inputs, _elements, _packets, _patch_header, inspect_video
from .trailer import require_coverage, run


def number(value, *, minimum=0, maximum=600):
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError('时间或尺寸超出允许范围')
    return result


def absolute_time(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('场次时间必须带时区')
    return stamp.timestamp()


def aspect(value):
    if value is None:
        return None
    result = Fraction(str(value).replace(':', '/'))
    if result <= 0 or result > 10 or result < Fraction(1, 10):
        raise ValueError('显示比例无效')
    return result


def dimensions(value):
    if value is None:
        return None
    parts = str(value).lower().split('x')
    if len(parts) != 2 or any(not p.isdigit() for p in parts):
        raise ValueError('画布格式须为宽x高')
    pair = tuple(int(p) for p in parts)
    if any(n < 2 or n > 4096 or n % 2 for n in pair):
        raise ValueError('画布尺寸须为2至4096的偶数')
    return pair


def inspect_clip(path, start, duration, *, display_aspect=None, receipt=None):
    """有界读取目标区间实际帧/包；不调用会写来源目录的prepare_video。"""
    path = Path(path).resolve(strict=True)
    before = file_stat(path)
    metadata = json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                               '-of', 'json', str(path)]).stdout)
    videos = [s for s in metadata['streams'] if s['codec_type'] == 'video']
    audios = [s for s in metadata['streams'] if s['codec_type'] == 'audio']
    if len(videos) != 1 or len(audios) != 1:
        raise ValueError('底层素材需要单路视频和单路音频')
    video, audio = videos[0], audios[0]
    if any(s.get('side_data_type') == 'Display Matrix' for s in video.get('side_data_list', [])):
        raise ValueError('请先明确处理底层素材的旋转矩阵')
    total = float(metadata['format']['duration'])
    if start < 0 or start + duration > total + .06:
        raise ValueError('请求区间超出底层素材，不能静默截短动画')
    if CAPTURE.fullmatch(path.name):
        report = inspect_video(path)
        if report['status'] != 'unchanged':
            raise ValueError('原片参数需要恢复或片内切换；先显式准备独立可读副本再渲染')
    correction = aspect(display_aspect)
    if receipt:
        legacy = next(s for s in receipt['source_info']['streams'] if s['codec_type'] == 'video')
        declared = legacy['width'], legacy['height']
        coded = legacy.get('coded_width', declared[0]), legacy.get('coded_height', declared[1])
        if (declared != coded and not receipt.get('codec_parameters')
                and not receipt.get('validation') and correction is None):
            raise ValueError('旧归档容器与编码画幅冲突；须用--base-display-aspect明确校正比例')
    origin = float(metadata['format'].get('start_time', 0))
    interval = f'{max(0, origin + start - 2):.6f}%{origin + start + duration + .15:.6f}'
    frames = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                             '-read_intervals', interval, '-show_frames', '-show_entries',
                             'frame=width,height,sample_aspect_ratio,best_effort_timestamp_time',
                             '-of', 'json', str(path)]).stdout)['frames']
    selected = [f for f in frames if start - .06 <=
                float(f.get('best_effort_timestamp_time', '-inf')) - origin < start + duration]
    shapes = {(f['width'], f['height'], f.get('sample_aspect_ratio', '1:1')) for f in selected}
    if len(shapes) != 1:
        raise ValueError('目标区间没有稳定的单一画幅；片内切换须先拆成独立输入')
    width, height, sar = shapes.pop()
    ratio = correction or Fraction(width, height) * aspect(sar if sar not in ('N/A', '0:1') else '1:1')
    if max(width, height) > 8192:
        raise ValueError('底层解码尺寸超限')
    packets = json.loads(run(['ffprobe', '-v', 'error', '-read_intervals', interval,
                              '-show_packets', '-show_entries', 'packet=stream_index,pts_time,duration_time',
                              '-of', 'json', str(path)]).stdout)['packets']
    spans = {}
    for kind, stream in (('video', video), ('audio', audio)):
        ranges = []
        for packet in packets:
            if packet['stream_index'] != stream['index'] or 'pts_time' not in packet:
                continue
            pts = float(packet['pts_time']) - origin
            length = float(packet.get('duration_time', 0))
            if length > 0:
                ranges.append((pts, pts + length))
        spans[kind] = sorted(ranges)
    require_coverage(spans, start, start + duration)
    if file_stat(path) != before:
        raise ValueError('检查期间底层素材发生变化')
    return {'path': str(path), 'stat': before, 'start': start, 'duration': duration,
            'decoded_size': [width, height], 'display_aspect': str(ratio),
            'aspect_corrected': correction is not None}


def select_rows(rows, start, end):
    """每个墙钟区间必须恰好对应一个来源；重叠及缺口均不猜测。"""
    relevant = sorted([r for r in rows if r['wall_start'] < end and
                       r['wall_start'] + r['duration_sec'] > start], key=lambda r: r['wall_start'])
    cursor, selected = start, []
    for row in relevant:
        left, right = max(start, row['wall_start']), min(end, row['wall_start'] + row['duration_sec'])
        if abs(left - cursor) > .06:
            raise ValueError('录像清单存在缺口或来源重叠，不能改变时间线')
        selected.append((row, left - row['wall_start'], right - left))
        cursor = right
    if not selected or abs(cursor - end) > .06:
        raise ValueError('录像覆盖不足；直播中的场次请等待分片封口后使用明确的--base输入')
    return selected


def session_config(session):
    root = Path(session).resolve(strict=True)
    return root, json.loads((root / 'config.json').read_text())


def source_clips(*, base=None, session=None, at, before, duration, display_aspect=None):
    if base is not None:
        path = Path(base).resolve(strict=True)
        saved = None
        # A caller passing a known archived output must retain legacy aspect warnings.
        receipt = path.with_suffix('.json')
        if receipt.is_file() and path.name.endswith('.mkv.mp4'):
            saved = archive_receipt(path.parent.parent / path.name.removesuffix('.mp4'))
        return [inspect_clip(path, number(at, maximum=10**9) - before, duration,
                             display_aspect=display_aspect, receipt=saved)]
    root, _ = session_config(session)
    manifest = json.loads((root / 'offline/manifest.json').read_text())
    start = absolute_time(at) - before
    clips = []
    for row, offset, length in select_rows(manifest['rows'], start, start + duration):
        source = Path(row['path']).resolve()
        saved = None
        if source.is_file():
            if file_stat(source) != {key: row[key] for key in ('size', 'mtime_ns')}:
                raise ValueError('原始录像与冻结清单不符')
            media = source
        else:
            media, saved = archived_input(row), archive_receipt(source)
        clip = inspect_clip(media, offset, length, display_aspect=display_aspect, receipt=saved)
        if saved:
            clip['archive_receipt'] = {'path': str(receipt_path(source)),
                                       'stat': file_stat(receipt_path(source))}
        clips.append(clip)
    return clips


def prepare_base(source, output):
    """只将可信参数头装入显式输出副本；不改生产缓存，不冒称整片已验证。"""
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve()
    if output == source.parent or output.is_relative_to(source.parent):
        raise ValueError('解码准备目录必须位于原始录像目录之外')
    report = inspect_video(source)
    before = file_stat(source)
    output.mkdir(parents=True, exist_ok=False)
    target = output / ('prepared' + source.suffix)
    patch = report['status'] in ('repairable', 'mixed_parameters') and report['initial_avcc'] != report['header_avcc']
    if patch:
        _check_inputs(report)
    old, initial = (bytes.fromhex(report[key]) for key in ('header_avcc', 'initial_avcc')) if patch else (b'', b'')
    with source.open('rb') as src, target.open('xb') as dst:
        if patch and len(initial) > len(old):
            _expanded_matroska(src, dst, old, initial)
        else:
            if patch:
                dst.write(_patch_header(src.read(65536), old, initial))
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    if file_stat(source) != before:
        raise ValueError('准备期间原始录像发生变化')
    if report['status'] != 'unmanaged':
        _check_inputs(report)
    if _packets(source) != _packets(target):
        raise ValueError('准备副本的媒体包或时间戳与原片不符')
    result = {'prepared': str(target), 'source': str(source), 'source_stat': before,
              'parameters': report, 'header_patched': patch, 'sha256': sha_file(target),
              'full_file_decode_verified': False,
              'next_step': '用prepared文件及目标相对秒数生成plan；仍须检查目标区间的稳定画幅与覆盖'}
    write_json(output / 'preparation.json', result)
    return result


def _expanded_matroska(src, dst, old, initial):
    """允许更长AVCC的独立副本：重建Tracks长度，丢弃失效索引，媒体Cluster逐字节复制。"""
    def size_bytes(value):
        width = next(n for n in range(1, 9) if value < (1 << (7 * n)) - 1)
        return ((1 << (7 * width)) | value).to_bytes(width, 'big')

    def header():
        first = src.read(1)
        if not first:
            return None
        if not first[0]:
            raise ValueError('EBML元素ID无效')
        width = 9 - first[0].bit_length()
        if width > 4:
            raise ValueError('EBML元素ID超限')
        ident = first + src.read(width - 1)
        first = src.read(1)
        if not first or not first[0]:
            raise ValueError('EBML长度缺失')
        count = 9 - first[0].bit_length()
        raw = first + src.read(count - 1)
        if len(raw) != count or len(ident) != width:
            raise ValueError('EBML头不完整')
        size = int.from_bytes(raw, 'big') & ((1 << (7 * count)) - 1)
        return ident, size, count, ident + raw

    def copy_bytes(count, write=True):
        while count:
            block = src.read(min(count, 1024 * 1024))
            if not block:
                raise ValueError('Matroska元素提前结束')
            if write:
                dst.write(block)
            count -= len(block)

    matches = 0

    def tracks(data):
        nonlocal matches
        result = bytearray()
        for begin, ident, payload, end in _elements(data, 0, len(data)):
            if end > len(data):
                raise ValueError('Tracks元素越界')
            body = data[payload:end]
            if ident == 0xbf:  # ancestor CRC no longer valid after header growth
                continue
            if ident == 0xae:
                body = tracks(body)
            elif ident == 0x63a2 and body == old:
                body = initial
                matches += 1
            identity = ident.to_bytes((ident.bit_length() + 7) // 8, 'big')
            result.extend(identity + size_bytes(len(body)) + body)
        return bytes(result)

    item = header()
    if item is None or item[0] != bytes.fromhex('1a45dfa3'):
        raise ValueError('需要标准Matroska EBML头')
    dst.write(item[3])
    copy_bytes(item[1])
    item = header()
    if item is None or item[0] != bytes.fromhex('18538067'):
        raise ValueError('缺少Matroska Segment')
    # Streaming unknown-sized Segment: stale SeekHead/Cues offsets are omitted below.
    dst.write(item[0] + b'\x01' + b'\xff' * 7)
    while (item := header()) is not None:
        ident, size, width, raw = item
        if size == (1 << (7 * width)) - 1:
            raise ValueError('不支持未知长度的Segment子元素')
        if ident in (bytes.fromhex('114d9b74'), bytes.fromhex('1c53bb6b'), b'\xbf'):
            copy_bytes(size, write=False)
        elif ident == bytes.fromhex('1654ae6b'):
            if size > 1024 * 1024:
                raise ValueError('Tracks头超出1MiB上限')
            body = src.read(size)
            if len(body) != size:
                raise ValueError('Tracks头不完整')
            body = tracks(body)
            dst.write(ident + size_bytes(len(body)) + body)
        else:
            dst.write(raw)
            copy_bytes(size)
    if matches != 1:
        raise ValueError('不能唯一定位需要替换的CodecPrivate')
