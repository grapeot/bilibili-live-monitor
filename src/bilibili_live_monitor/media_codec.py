"""PK 参数感知的离线视频读取；原片只读，恢复副本不重编码。

问题背景参考录播姬：https://rec.danmuji.org/dev/repair/
仅处理本录制器的 H264 MKV 分片；分片内参数切换明确拒绝固定画幅导出。
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

POLICY = 'h264-segment-parameters-v1'
CAPTURE = re.compile(r'([a-f0-9]{8}-\d{8}T\d{6}Z-[a-f0-9]{8})-(\d{5})\.mkv')


class CodecParametersError(ValueError):
    """不能证明安全恢复时保留来源，不能按猜测参数继续压缩。"""


def _run(args):
    return subprocess.run(args, check=True, capture_output=True, timeout=180)


def _stat(path):
    stat = path.stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _avcc(data):
    if len(data) < 7 or data[0] != 1 or data[5] & 31 != 1:
        raise CodecParametersError('仅支持单组AVC SPS/PPS')
    size = int.from_bytes(data[6:8], 'big')
    sps, pos = data[8:8 + size], 8 + size
    if len(sps) != size or pos >= len(data) or data[pos] != 1:
        raise CodecParametersError('AVC参数不完整或有多组PPS')
    size = int.from_bytes(data[pos + 1:pos + 3], 'big')
    end = pos + 3 + size
    pps = data[pos + 3:end]
    if not sps or not pps or len(pps) != size or sps[0] & 31 != 7 or pps[0] & 31 != 8:
        raise CodecParametersError('AVC SPS/PPS无效')
    return (data[4] & 3) + 1, (sps, pps), data[end:]


def _make_avcc(original, pair):
    length, _, tail = _avcc(original)
    sps, pps = pair
    return (b'\x01' + sps[1:4] + bytes([0xfc | (length - 1), 0xe1])
            + len(sps).to_bytes(2, 'big') + sps + b'\x01'
            + len(pps).to_bytes(2, 'big') + pps + tail)


@lru_cache(maxsize=256)
def _scan(name, size, mtime):
    """只缓存绑定stat的码流摘要，不把容器旧参数当成新参数插入。"""
    path = Path(name)
    result = _run(['ffprobe', '-v', 'fatal', '-show_streams', '-show_data', '-of', 'json', name])
    videos = [s for s in json.loads(result.stdout)['streams'] if s['codec_type'] == 'video']
    if len(videos) != 1 or videos[0].get('codec_name') != 'h264':
        raise CodecParametersError('录制分片不是单路H264视频')
    dump = videos[0]['extradata']
    header = bytes.fromhex(''.join(line.split(': ', 1)[1].split('  ', 1)[0].replace(' ', '')
                                  for line in dump.strip().splitlines()))
    _avcc(header)
    raw = _run(['ffmpeg', '-nostdin', '-v', 'fatal', '-i', name, '-map', '0:v:0', '-c', 'copy',
                '-bsf:v', 'filter_units=pass_types=7|8', '-f', 'data', '-']).stdout
    if len(raw) > 4 * 1024 * 1024:
        raise CodecParametersError('参数事件超出有界扫描范围')
    units = tuple(unit for unit in re.split(b'\x00\x00\x00?\x01', raw) if unit)
    if raw and not raw.startswith((b'\x00\x00\x01', b'\x00\x00\x00\x01')):
        raise CodecParametersError('不支持此FFmpeg的参数输出格式')
    if any(unit[0] & 31 not in (7, 8) for unit in units):
        raise CodecParametersError('参数NAL解析失败')
    if _stat(path) != {'size': size, 'mtime_ns': mtime}:
        raise CodecParametersError('扫描期间原片发生变化')
    return header, units


def _scan_file(path):
    stat = _stat(path)
    header, units = _scan(str(path), stat['size'], stat['mtime_ns'])
    return header, units, stat


def _decode(path, *, short=False):
    args = ['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '2', '-i', str(path)]
    if short:
        args += ['-t', '1']
    result = subprocess.run([*args, '-map', '0:v:0', '-map', '0:a?', '-f', 'null', '-'],
                            capture_output=True, timeout=180)
    return result.returncode == 0 and not result.stderr


def _history(source, match, limit=32):
    found, dependencies = {}, []
    number = int(match[2])
    for n in range(number - 1, max(-1, number - limit - 1), -1):
        previous = source.with_name(f'{match[1]}-{n:05}.mkv')
        if previous.is_file():
            header, units, stat = _scan_file(previous)
            dependencies.append({'source': str(previous), 'stat': stat})
            for unit in reversed(units):
                found.setdefault(unit[0] & 31, unit)
            if n == 0 and not (7 in found and 8 in found):
                # The first capture header is the only non-in-band anchor.
                if not _decode(previous, short=True):
                    raise CodecParametersError('起始分片不能解码，不能猜测参数')
                _, pair, _ = _avcc(header)
                found.setdefault(7, pair[0])
                found.setdefault(8, pair[1])
        else:
            # A preceding original may already have been compressed/trashed.
            # Only use metadata bound to an accepted archive receipt/output.
            from .archive_media import archive_receipt
            try:
                saved = archive_receipt(previous)
                codec = saved['codec_parameters']
                if codec['policy'] != POLICY:
                    raise ValueError('参数记录版本不匹配')
                _, pair, _ = _avcc(bytes.fromhex(codec['final_avcc']))
            except (OSError, ValueError, KeyError) as error:
                raise CodecParametersError('缺少可信的前序参数：' + previous.name) from error
            found.setdefault(7, pair[0])
            found.setdefault(8, pair[1])
            dependencies.append({'archive': str(previous), 'output_stat': saved['output_stat']})
        if 7 in found and 8 in found:
            return (found[7], found[8]), dependencies
    raise CodecParametersError('有界历史内找不到完整参数，保留原片')


def inspect_video(source):
    """只读：不建目录、不写缓存；压缩数据扫描，不做全片解码。"""
    source = Path(source).resolve(strict=True)
    match = CAPTURE.fullmatch(source.name)
    if not match:
        return {'policy': POLICY, 'status': 'unmanaged', 'source': str(source)}
    header, units, stat = _scan_file(source)
    _, initial, _ = _avcc(header)
    dependencies = []
    if not _decode(source, short=True):
        initial, dependencies = _history(source, match)
    active = initial
    changed = False
    for unit in units:
        pair = (unit, active[1]) if unit[0] & 31 == 7 else (active[0], unit)
        changed |= pair != active
        active = pair
    status = ('mixed_parameters' if changed else
              'repairable' if initial != _avcc(header)[1] else 'unchanged')
    if _stat(source) != stat:
        raise CodecParametersError('检查期间原片发生变化')
    return {'policy': POLICY, 'status': status, 'source': str(source), 'source_stat': stat,
            'initial_avcc': _make_avcc(header, initial).hex(),
            'final_avcc': _make_avcc(header, active).hex(), 'header_avcc': header.hex(),
            'dependencies': dependencies}


def _check_inputs(report):
    if _stat(Path(report['source'])) != report['source_stat']:
        raise CodecParametersError('原片发生变化')
    for row in report['dependencies']:
        if 'source' in row:
            if _stat(Path(row['source'])) != row['stat']:
                raise CodecParametersError('前序参数来源发生变化')
        else:
            from .archive_media import archive_receipt
            if archive_receipt(row['archive'])['output_stat'] != row['output_stat']:
                raise CodecParametersError('前序归档来源发生变化')


def _packets(path):
    raw = _run(['ffprobe', '-v', 'fatal', '-show_packets', '-show_data_hash', 'sha256',
                '-show_entries', 'packet=stream_index,pts_time,dts_time,duration_time,data_hash',
                '-of', 'json', str(path)]).stdout
    return hashlib.sha256(json.dumps(json.loads(raw)['packets'], sort_keys=True).encode()).hexdigest()


def _elements(data, start, end):
    """有界EBML头解析；不进入Cluster，不扫描媒体payload。"""
    pos = start
    while pos < end and pos < len(data):
        begin = pos
        def vint(keep_marker):
            nonlocal pos
            if pos >= len(data) or not data[pos]:
                raise CodecParametersError('EBML头不完整')
            width = 9 - data[pos].bit_length()
            if width > (4 if keep_marker else 8) or pos + width > len(data):
                raise CodecParametersError('EBML长度不受支持')
            value = int.from_bytes(data[pos:pos + width], 'big')
            pos += width
            return value if keep_marker else value & ((1 << (7 * width)) - 1)
        element_id, size = vint(True), vint(False)
        finish = pos + size
        if finish > end:
            raise CodecParametersError('EBML元素越界')
        yield begin, element_id, pos, finish
        pos = finish


def _patch_header(head, old, initial):
    # Keep all element sizes and offsets. Remove only ancestor CRC declarations
    # invalidated by the CodecPrivate update, replacing them with equal-size Void.
    # Ordinary ffmpeg remux can discard an AAC priming packet, so it is unsuitable
    # for this exact-packet-preserving preparation step.
    matches = []
    masters = {0x18538067, 0x1654ae6b, 0xae}  # Segment / Tracks / TrackEntry
    def walk(start, end, ancestors):
        for begin, element_id, payload, finish in _elements(head, start, end):
            if element_id in masters:
                if element_id != 0x18538067 and finish > len(head):
                    raise CodecParametersError('Tracks超出有界头部空间')
                walk(payload, finish, [*ancestors, (payload, finish)])
            elif element_id == 0x63a2 and head[payload:finish] == old:
                matches.append((payload, finish, ancestors))
    # Segment may use an unknown (all-ones) size; the declared virtual bound is
    # permitted, but iteration still stops at the available 64KiB header.
    walk(0, 1 << 63, [])
    if len(matches) != 1:
        raise CodecParametersError('不能唯一定位Tracks内的CodecPrivate')
    start, end, ancestors = matches[0]
    patched = bytearray(head)
    patched[start:end] = initial.ljust(end - start, b'\x00')
    for start, end in ancestors:
        for begin, element_id, payload, finish in _elements(head, start, end):
            if element_id == 0xbf:
                if finish - payload != 4:
                    raise CodecParametersError('CRC32元素长度无效')
                patched[begin] = 0xec  # Void, same one-byte ID and payload size
    return bytes(patched)


def prepare_video(source, *, expected_sha=None):
    """返回(可解码路径, 参数报告)。只创建私有副本，正常来源不复制。"""
    source = Path(source).resolve(strict=True)
    report = inspect_video(source)
    if expected_sha is not None and _sha(source) != expected_sha:
        raise CodecParametersError('源片指纹不符')
    if report['status'] == 'mixed_parameters':
        raise CodecParametersError('分片内部编码参数变化，须按参数边界拆分；保留原片')
    if report['status'] != 'repairable':
        return source, report
    _check_inputs(report)
    source_sha = _sha(source)
    initial = bytes.fromhex(report['initial_avcc'])
    old = bytes.fromhex(report['header_avcc'])
    if len(initial) > len(old):
        raise CodecParametersError('新参数超出原CodecPrivate空间，保留原片')
    if shutil.disk_usage(source.parent).free < 2 * source.stat().st_size + 64 * 1024**2:
        raise CodecParametersError('空间不足以同时保留原片与恢复临时副本')
    key = hashlib.sha256((POLICY + source_sha + initial.hex()).encode()).hexdigest()
    root = source.parent / '.codec-media'
    root.mkdir(exist_ok=True)
    target, receipt = root / f'{key}.mkv', root / f'{key}.json'
    with (root / f'{key}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_file() and receipt.is_file():
            saved = json.loads(receipt.read_text())
            if (saved.get('verified') is True and saved.get('source_sha256') == source_sha
                    and saved.get('policy') == POLICY
                    and saved.get('output_sha256') == _sha(target)):
                _check_inputs(report)
                return target, report
            raise CodecParametersError('恢复缓存校验失败，不覆盖已有文件')
        if target.exists() or receipt.exists():
            raise CodecParametersError('存在未验收恢复副本，不覆盖')
        fd, name = tempfile.mkstemp(prefix='.partial-', suffix='.mkv', dir=root)
        temporary = Path(name)
        try:
            with source.open('rb') as src, os.fdopen(fd, 'wb') as dst:
                head = src.read(65536)
                dst.write(_patch_header(head, old, initial))
                while block := src.read(1024 * 1024):
                    dst.write(block)
            if not _decode(temporary) or _packets(source) != _packets(temporary):
                raise CodecParametersError('恢复副本解码或原码时间戳校验失败')
            _check_inputs(report)
            if _sha(source) != source_sha:
                raise CodecParametersError('恢复期间原片发生变化')
            os.link(temporary, target)
            saved = {**report, 'source_sha256': source_sha, 'output_sha256': _sha(target),
                     'output': str(target), 'verified': True}
            with receipt.open('x') as stream:
                json.dump(saved, stream, ensure_ascii=False)
        finally:
            temporary.unlink(missing_ok=True)
    return target, report


def configure_parser(commands):
    parser = commands.add_parser('media-codec', help='PK视频参数检查与只读来源恢复')
    parser.add_argument('action', choices=('inspect', 'prepare'))
    parser.add_argument('--source', required=True, type=Path)


def main(args):
    if args.action == 'inspect':
        result = inspect_video(args.source)
    else:
        path, result = prepare_video(args.source)
        result = {**result, 'media_input': str(path)}
    print(json.dumps(result, ensure_ascii=False))
    return 0
