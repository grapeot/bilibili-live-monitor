"""原片保留凭证；与成功回执隔离，材料变化后才重新检查参数阻塞。"""
import hashlib
import json
import math
from pathlib import Path

from . import archive_validation, media_codec
from .archive_media import archive_path, file_stat, receipt_path

ARCHIVE_POLICY = 'hevc-2800k-v2'
VIDEO_BITRATE = 2_800_000
PARAMETER_BLOCKS = {'missing_parameter_history', 'ambiguous_parameters',
                    'unresolved_decode_error', 'unsupported_container'}


class ArchiveSkip(ValueError):
    """已识别的素材限制；运行环境和输出错误不得记成素材跳过。"""

    def __init__(self, reason, message, *, warning=False):
        super().__init__(message)
        self.reason, self.warning = reason, warning


def skip_path(source):
    source = Path(source)
    return source.with_name(source.name + '.archive-skip.json')


def parameter_dependent(reason):
    return reason.startswith('codec_') or reason in (
        'source_decode_error', 'mixed_codec_parameters', 'unsupported_profile',
        'unsupported_streams', 'source_timestamps', 'invalid_duration')


def dependency_fingerprint(source):
    """与参数回查相同的512片上界，只stat文件，不扫描媒体或写缓存。"""
    source = Path(source)
    match = media_codec.CAPTURE.fullmatch(source.name)
    rows = []
    if match:
        number = int(match[2])
        for n in range(number - 1, max(-1, number - 513), -1):
            previous = source.with_name(f'{match[1]}-{n:05}.mkv')
            row = []
            for path in (previous, receipt_path(previous), archive_path(previous)):
                try:
                    stat = file_stat(path)
                except FileNotFoundError:
                    stat = None
                row.append(stat)
            rows.append(row)
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def reusable_skip(saved, item):
    if not (saved.get('source') == item['source'] and saved.get('source_stat') == item['source_stat']
            and saved.get('policy') == ARCHIVE_POLICY and saved.get('status') == 'skipped'):
        return False
    if 'dependencies' in saved or parameter_dependent(saved.get('reason', '')):
        return (saved.get('codec_policy') == archive_validation.POLICY
                and saved.get('parameter_policy') == media_codec.POLICY
                and saved.get('dependencies') == dependency_fingerprint(item['source']))
    return True


def execution_error(error):
    stderr = getattr(error, 'stderr', b'') or b''
    text = (stderr.decode(errors='replace') if isinstance(stderr, bytes) else str(stderr)).lower()
    return any(marker in text for marker in (
            'no space left', 'permission denied', 'operation not permitted', 'input/output error',
            'cannot allocate memory', 'resource temporarily unavailable', 'encoder unavailable',
            'encoder not found', 'error while opening encoder', 'error opening output',
            'error while opening output', 'failed to create compression session', 'no device available'))


def decode_error(error):
    if execution_error(error):
        return False
    stderr = getattr(error, 'stderr', b'') or b''
    text = (stderr.decode(errors='replace') if isinstance(stderr, bytes) else str(stderr)).lower()
    return any(marker in text for marker in (
        'corrupt decoded frame', 'error while decoding', 'invalid data found when processing input',
        'invalid nal unit', 'moov atom not found', 'error parsing nal unit', 'co located pocs unavailable',
        'non-existing pps', 'decode_slice_header error', 'reference picture missing',
        'left block unavailable', 'cabac decode', 'concealing', 'mmco: unref short failure'))


def check_saving(info, source_size, audio):
    duration = float(info.get('format', {}).get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ArchiveSkip('invalid_duration', '原片时长无效，保留原片', warning=True)
    expected_bytes = duration * (VIDEO_BITRATE + max(int(audio.get('bit_rate') or 128_000), 0)) / 8
    if source_size <= expected_bytes * 1.05:
        raise ArchiveSkip('low_bitrate', '原片码率低于目标总码率或预计节省不足5%，跳过转码')
