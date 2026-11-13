"""PK 参数感知的离线视频读取；原片只读，恢复副本不重编码。

问题背景参考录播姬：https://rec.danmuji.org/dev/repair/
仅处理本录制器的 H264 MKV 分片；分片内参数切换明确拒绝固定画幅导出。
"""
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

POLICY = 'h264-segment-parameters-v2'
HISTORY_POLICIES = {'h264-segment-parameters-v1', POLICY}
CAPTURE = re.compile(r'([a-f0-9]{8}-\d{8}T\d{6}Z-[a-f0-9]{8})-(\d{5})\.mkv')


class CodecParametersError(ValueError):
    """不能证明安全恢复时保留来源，不能按猜测参数继续压缩。"""

    def __init__(self, message, *, reason='unsupported_parameters'):
        super().__init__(message)
        self.reason = reason


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
    # A null muxer can reject duplicate DTS even when every video frame decodes.
    # Decode frames without an output muxer. Timestamp/AAC validation belongs to
    # the archive contract; decoder stderr remains a hard failure here.
    result = _frame_probe(path, short=short)
    if result.returncode or result.stderr:
        logging.getLogger(__name__).warning('解码检查失败：%s', result.stderr.decode(errors='replace')[-1000:])
    return result.returncode == 0 and not result.stderr


def _frame_probe(path, *, short=False):
    return subprocess.run(['ffprobe', '-v', 'error', '-threads', '2',
                           *(['-read_intervals', '%+1'] if short else []),
                           '-select_streams', 'v:0', '-show_frames', '-show_entries',
                           'frame=width,height,sample_aspect_ratio,best_effort_timestamp_time',
                           '-of', 'json', str(path)], capture_output=True, timeout=180)


def _valid_candidate(path):
    result = _frame_probe(path)
    from .archive_policy import decode_error
    error = subprocess.CalledProcessError(result.returncode, 'ffprobe', stderr=result.stderr)
    if result.returncode < 0:
        raise error
    if result.returncode or result.stderr:
        if decode_error(error):
            return False
        raise error
    frames = json.loads(result.stdout).get('frames', [])
    if not frames or not all(f.get('width', 0) > 0 and f.get('height', 0) > 0 for f in frames):
        raise ValueError('探测未产生可信的视频帧，不能作为选参反证')
    return True


def _leading_parameters(source, header):
    """只认可首个视频包内、首个VCL之前的完整SPS/PPS，不借容器补齐。"""
    result = _run(['ffprobe', '-v', 'fatal', '-select_streams', 'v:0', '-read_intervals', '%+#1',
                   '-show_packets', '-show_data', '-of', 'json', str(source)])
    packets = json.loads(result.stdout).get('packets', [])
    if not packets:
        return None
    dump = packets[0].get('data', '')
    raw = bytes.fromhex(''.join(line.split(': ', 1)[1].split('  ', 1)[0].replace(' ', '')
                               for line in dump.strip().splitlines()))
    length, _, _ = _avcc(header)
    found, pos = {}, 0
    while pos < len(raw):
        if pos + length > len(raw):
            return None
        size = int.from_bytes(raw[pos:pos + length], 'big')
        pos += length
        unit = raw[pos:pos + size]
        if not unit or len(unit) != size:
            return None
        pos += size
        kind = unit[0] & 31
        if kind in range(1, 6):
            return (found[7], found[8]) if 7 in found and 8 in found else None
        if kind in (7, 8):
            found[kind] = unit
    return None


def _history(source, match, limit=512):
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
                if codec['policy'] not in HISTORY_POLICIES:
                    raise ValueError('参数记录版本不匹配')
                _, pair, _ = _avcc(bytes.fromhex(codec['final_avcc']))
            except (FileNotFoundError, ValueError, KeyError) as error:
                raise CodecParametersError('缺少可信的前序参数：' + previous.name,
                                           reason='missing_parameter_history') from error
            found.setdefault(7, pair[0])
            found.setdefault(8, pair[1])
            dependencies.append({'archive': str(previous), 'output_stat': saved['output_stat']})
        if 7 in found and 8 in found:
            return (found[7], found[8]), dependencies
    raise CodecParametersError('有界历史内找不到完整参数，保留原片', reason='missing_parameter_history')


def inspect_video(source):
    """只读：不建目录、不写缓存；压缩数据扫描，不做全片解码。"""
    source = Path(source).resolve(strict=True)
    match = CAPTURE.fullmatch(source.name)
    if not match:
        return {'policy': POLICY, 'status': 'unmanaged', 'source': str(source)}
    header, units, stat = _scan_file(source)
    _, initial, _ = _avcc(header)
    dependencies = []
    # Decode success is NOT proof of a correct SPS (a smaller old height can
    # silently crop the decoded picture). Always establish the capture epoch.
    if int(match[2]) > 0:
        leading = _leading_parameters(source, header)
        if leading is None:
            initial, dependencies = _history(source, match)
        else:
            initial = leading
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


def _copy_with_header(source, target, report):
    old, initial = (bytes.fromhex(report[key]) for key in ('header_avcc', 'initial_avcc'))
    with source.open('rb') as src, target.open('xb') as dst:
        if len(initial) > len(old):
            from .media_container import expanded_matroska
            try:
                expanded_matroska(src, dst, old, initial)
            except ValueError as error:
                raise CodecParametersError(str(error), reason='unsupported_container') from error
        else:
            dst.write(_patch_header(src.read(65536), old, initial))
            shutil.copyfileobj(src, dst, 1024 * 1024)


def _select_candidate(source, temporary, report):
    """Validate both explanations for a conflicting header; do not guess.

    A successful native decode alone can silently crop rows with a stale SPS.
    Conversely, inherited history can be stale after an unrecorded epoch switch.
    If both decode, neither explanation is proved: keep the original blocked.
    """
    _copy_with_header(source, temporary, report)
    leading = _leading_parameters(source, bytes.fromhex(report['header_avcc']))
    if leading is not None and leading == _avcc(bytes.fromhex(report['initial_avcc']))[1]:
        if not _valid_candidate(temporary):
            raise CodecParametersError('自带参数副本完整视频解码失败，保留原片',
                                       reason='unresolved_decode_error')
        return temporary, {**report, 'parameter_selection': {'basis': 'complete_inband_before_first_vcl'}}
    native_ok, history_ok = _valid_candidate(source), _valid_candidate(temporary)
    evidence = {'native_decode_ok': native_ok, 'history_decode_ok': history_ok,
                'scope': 'all video frames; decoder stderr rejected; no output muxer'}
    if native_ok and history_ok:
        raise CodecParametersError('两套不同参数均可解码，不能排除静默裁画面；保留原片',
                                   reason='ambiguous_parameters')
    if not native_ok and not history_ok:
        raise CodecParametersError('原生和历史参数均未通过完整视频解码，保留原片',
                                   reason='unresolved_decode_error')
    report = {**report, 'parameter_selection': evidence}
    if history_ok:
        return temporary, report
    # Only the native explanation passes. Rebuild the final state from that
    # initial header instead of carrying a stale historical final_avcc forward.
    header, units, _ = _scan_file(source)
    _, active, _ = _avcc(header)
    changed = False
    for unit in units:
        pair = (unit, active[1]) if unit[0] & 31 == 7 else (active[0], unit)
        changed |= pair != active
        active = pair
    report.update(initial_avcc=header.hex(), final_avcc=_make_avcc(header, active).hex(),
                  status='mixed_parameters' if changed else 'unchanged')
    return source, report


def prepare_copy(source, output):
    """Explicit private copy for offline callers; never writes a source cache."""
    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    if output.is_relative_to(source.parent):
        raise ValueError('准备副本必须位于原片目录之外')
    report = inspect_video(source)
    if output.exists():
        raise FileExistsError(output)
    owned = False
    try:
        with tempfile.TemporaryDirectory(prefix='.parameters-', dir=output.parent) as name:
            selected = source
            if report['status'] in ('repairable', 'mixed_parameters') and report['header_avcc'] != report['initial_avcc']:
                _check_inputs(report)
                selected, report = _select_candidate(source, Path(name) / 'candidate.mkv', report)
            with selected.open('rb') as src, output.open('xb') as dst:
                owned = True
                shutil.copyfileobj(src, dst, 1024 * 1024)
        if report['status'] != 'unmanaged':
            _check_inputs(report)
        if _packets(source) != _packets(output):
            raise CodecParametersError('准备副本的媒体包或时间戳不符', reason='packet_mismatch')
        return report
    except BaseException:
        if owned:
            output.unlink(missing_ok=True)
        raise


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


def prepare_video(source, *, expected_sha=None, allow_mixed=False):
    """返回(可解码路径, 参数报告)。只创建私有副本，正常来源不复制。"""
    source = Path(source).resolve(strict=True)
    report = inspect_video(source)
    if expected_sha is not None and _sha(source) != expected_sha:
        raise CodecParametersError('源片指纹不符')
    if report['status'] == 'mixed_parameters' and not allow_mixed:
        raise CodecParametersError('分片内部编码参数变化，须按参数边界拆分；保留原片')
    if (report['status'] not in ('repairable', 'mixed_parameters')
            or report['header_avcc'] == report['initial_avcc']):
        return source, report
    _check_inputs(report)
    source_sha = _sha(source)
    initial = bytes.fromhex(report['initial_avcc'])
    if shutil.disk_usage(source.parent).free < 2 * source.stat().st_size + 64 * 1024**2:
        raise CodecParametersError('空间不足以同时保留原片与恢复临时副本')
    key = hashlib.sha256((POLICY + source_sha + initial.hex()
                          + (':mixed' if report['status'] == 'mixed_parameters' else '')).encode()).hexdigest()
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
                return target, {**report, **({'parameter_selection': saved['parameter_selection']}
                                           if 'parameter_selection' in saved else {})}
            raise CodecParametersError('恢复缓存校验失败，不覆盖已有文件')
        if target.exists() or receipt.exists():
            raise CodecParametersError('存在未验收恢复副本，不覆盖')
        name = tempfile.mkdtemp(prefix='.partial-', dir=root)
        temporary = Path(name) / 'candidate.mkv'
        try:
            selected, report = _select_candidate(source, temporary, report)
            if report['status'] == 'mixed_parameters' and not allow_mixed:
                raise CodecParametersError('分片内部编码参数变化，保留原片')
            if selected == source:
                _check_inputs(report)
                if _sha(source) != source_sha:
                    raise CodecParametersError('恢复期间原片发生变化')
                return source, report
            if _packets(source) != _packets(selected):
                raise CodecParametersError('恢复副本的原码时间戳校验失败', reason='packet_mismatch')
            _check_inputs(report)
            if _sha(source) != source_sha:
                raise CodecParametersError('恢复期间原片发生变化')
            os.link(selected, target)
            saved = {**report, 'source_sha256': source_sha, 'output_sha256': _sha(target),
                     'output': str(target), 'verified': True}
            with receipt.open('x') as stream:
                json.dump(saved, stream, ensure_ascii=False)
        finally:
            temporary.unlink(missing_ok=True)
            Path(name).rmdir()
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
