"""已完成直播录像的分段HEVC归档；默认只读，不改变分析和发布状态。"""
import fcntl
import json
import math
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from . import archive_validation as validation
from .archive_media import archive_path, archive_receipt, file_stat, receipt_path
from .live_share import atomic_json
from .media_codec import prepare_video


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, timeout=3600, **kwargs)


def probe(path, *, frames=False):
    args = ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json']
    if frames:
        args += ['-count_frames', '-show_frames', '-show_entries',
                 'frame=media_type,best_effort_timestamp_time,width,height,pix_fmt,sample_aspect_ratio,color_space,color_transfer,color_primaries']
    result = command([*args, str(path)])
    if frames and result.stderr:
        raise ValueError('视频帧解码报错，保留原片：' + result.stderr.decode(errors='replace')[-1000:])
    info = json.loads(result.stdout)
    if frames:
        # Retain only the bound, not thousands of frame records in the receipt.
        decoded = info.pop('frames', [])
        times = [f['best_effort_timestamp_time'] for f in decoded if f['media_type'] == 'video']
        info['timestamp_max_adjustment_us'] = timestamp_adjustment(times)
        info['video_evidence'] = validation.video_evidence(decoded)
    return info


def timestamp_adjustment(times):
    previous, maximum = None, 0
    if not times:
        raise ValueError('没有可验证的视频帧时间戳')
    for value in times:
        timestamp = round(Fraction(value) * 1_000_000)
        adjusted = timestamp if previous is None else max(timestamp, previous + 1)
        maximum = max(maximum, adjusted - timestamp)
        previous = adjusted
    return maximum


def streams(info):
    video = [s for s in info['streams'] if s['codec_type'] == 'video']
    audio = [s for s in info['streams'] if s['codec_type'] == 'audio']
    if len(video) != 1 or len(audio) != 1 or len(info['streams']) != 2:
        raise ValueError('首版仅支持一条视频和一条音轨')
    return video[0], audio[0]


def check_profile(info):
    video, audio = streams(info)
    rate = float(Fraction(video.get('avg_frame_rate', '0/1')))
    if (not (0 < video['width'] <= 1920 and 0 < video['height'] <= 1440)
            or video['width'] % 2 or video['height'] % 2 or video.get('pix_fmt') != 'yuv420p'
            or not any(abs(rate - approved) < .02 for approved in (30000 / 1001, 30, 60))
            or video.get('codec_name') != 'h264'
            or audio.get('codec_name') != 'aac'
            or any(video.get(key) != 'bt709' for key in ('color_space', 'color_transfer', 'color_primaries'))):
        raise ValueError('未验证的编码规格；仅支持H264 8-bit SDR BT709、最大1920x1440、29.97/30/60fps + AAC')


def verify_output(source_info, target):
    if source_info.get('timestamp_max_adjustment_us', 1001) > 1000:
        raise ValueError('原片时间戳需要超过1毫秒修正或未经验证，保留原片')
    info = probe(target, frames=True)
    before_v, before_a = streams(source_info)
    video, audio = streams(info)
    duration = float(info['format']['duration'])
    original_duration = float(source_info['format']['duration'])
    if not math.isfinite(duration) or duration <= 0 or abs(duration - original_duration) > .25:
        raise ValueError('归档时长不匹配')
    if (video.get('codec_name') != 'hevc'
            or any(video.get(key) != before_v.get(key) for key in ('width', 'height', 'pix_fmt', 'color_space', 'color_transfer', 'color_primaries'))
            or any(audio.get(key) != before_a.get(key) for key in ('codec_name', 'channels', 'sample_rate'))):
        raise ValueError('归档画面或音轨规格不匹配')
    # MKV/MP4 infer different avg/r_frame_rate for jitter or missing starts.
    # Keep frame timestamps (disambiguating duplicates by 1us), actual count and duration.
    original_frames = before_v.get('nb_read_frames')
    if original_frames in (None, 'N/A', '0'):
        raise ValueError('无法验证原片解码帧数，保留原片')
    if video.get('nb_read_frames') != original_frames:
        raise ValueError('归档帧数改变')
    if source_info.get('validation_policy') == validation.POLICY:
        validation.verify_evidence(source_info, info, target, command)
    command(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '2', '-i', str(target),
             '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'])
    return info


def no_writer(source):
    result = subprocess.run(['lsof', '-F', 'fa', str(source)], capture_output=True, text=True, timeout=20)
    if result.returncode not in (0, 1):
        raise ValueError('无法检查原片写入者：' + result.stderr)
    if any(line in ('aw', 'au') for line in result.stdout.splitlines()):
        raise ValueError('原片仍有写入者')


def inventory(root, session_ids=()):
    """仅读已有清单及stat；不创建目录、锁、缓存，不探测媒体。"""
    root = Path(root).resolve(strict=True)
    wanted = set(session_ids)
    found, items = set(), {}
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or wanted and directory.name not in wanted:
            continue
        config_path, state_path = directory / 'config.json', directory / 'workflow.json'
        if not config_path.is_file() or not state_path.is_file():
            continue
        found.add(directory.name)
        config, state = json.loads(config_path.read_text()), json.loads(state_path.read_text())
        manifest = directory / 'offline/manifest.json'
        if state.get('stage') != 'completed' or not manifest.is_file():
            continue
        recording_root = Path(config['recording_root']).resolve()
        for row in json.loads(manifest.read_text()).get('rows', []):
            source = Path(row['path']).resolve()
            if source in items:
                items[source]['sessions'].append(directory.name)
                continue
            item = {'source': str(source), 'output': str(archive_path(source)), 'sessions': [directory.name],
                    'source_stat': {key: row[key] for key in ('size', 'mtime_ns')}, 'duration': row.get('duration_sec'),
                    'action': 'encode', 'config': str(config_path), 'workflow': str(state_path), 'legacy_row': row}
            if source.suffix.lower() != '.mkv':
                item.update(action='skip', reason='audio_only_or_unsupported')
            elif not (source.is_relative_to(recording_root)
                      or source.is_relative_to((directory / 'offline/recovered').resolve())):
                item.update(action='skip', reason='external_media')
            elif receipt_path(source).exists():
                try:
                    saved = archive_receipt(source)
                    if saved['source_stat'] != item['source_stat']:
                        raise ValueError('旧来源不匹配')
                    item.update(action='archived', cleanup=saved.get('cleanup'))
                except (ValueError, OSError, KeyError) as error:
                    item.update(action='skip', reason=str(error))
            elif not source.is_file():
                item.update(action='skip', reason='missing_original')
            elif file_stat(source) != item['source_stat']:
                item.update(action='skip', reason='changed_original')
            elif archive_path(source).exists():
                item.update(action='skip', reason='unverified_output_exists')
            items[source] = item
    if wanted - found:
        raise ValueError('找不到场次：' + ','.join(sorted(wanted - found)))
    return list(items.values())


@contextmanager
def writer_lock(root):
    # 子ffmpeg继承同一个锁FD，父进程意外退出也不会同时启动第二个编码器。
    with (Path(root) / '.archive.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield lock.fileno()


def check_source(item):
    source = Path(item['source'])
    for workflow in item.get('workflows', [item['workflow']]):
        if json.loads(Path(workflow).read_text()).get('stage') != 'completed':
            raise ValueError('场次不再是completed')
    if file_stat(source) != item['source_stat'] or time.time() - source.stat().st_mtime < 10:
        raise ValueError('原片改变或刚刚写入')
    no_writer(source)


def encode(item, lock_fd):
    check_source(item)
    source, target = Path(item['source']), Path(item['output'])
    media_source, codec_parameters = prepare_video(source, expected_sha=item['legacy_row'].get('sha256'),
                                                   allow_mixed=True)
    mixed = codec_parameters['status'] == 'mixed_parameters'
    check_profile(probe(media_source))
    decoded_input = probe(media_source, frames=True)
    decoded_input['audio_evidence'] = validation.audio_evidence(media_source, command)
    source_info = validation.normalize_reference(decoded_input, mixed=mixed)
    if source_info['timestamp_max_adjustment_us'] > 1000:
        raise ValueError('原片时间戳需要超过1毫秒修正，保留原片')
    # 留出录制余量；Trash并不释放空间，不能计入可用空间。
    needed = max(source.stat().st_size, int(float(source_info['format']['duration']) * 500_000))
    if shutil.disk_usage(source.parent).free < 5 * 1024**3 + needed:
        raise ValueError('不足以保留5GiB余量并写入归档')
    target.parent.mkdir(exist_ok=True)
    temporary = target.with_name(f'.{target.stem}.{os.getpid()}.partial.mp4')
    if target.exists() or receipt_path(source).exists():
        raise ValueError('归档目标已存在，先inspect，不覆盖')
    try:
        command(['nice', '-n', '10', 'ffmpeg', '-hide_banner', '-nostdin', '-v', 'error', '-xerror', '-n',
                 '-threads', '2', '-filter_threads', '2', '-copyts',
                 *(['-reinit_filter', '0'] if mixed else []),
                 '-i', str(media_source), '-map', '0:v:0', '-map', '0:a:0',
                 '-vf', validation.MIXED_FILTER if mixed else validation.TIME_FILTER,
                 '-c:v', 'hevc_videotoolbox', '-allow_sw', '0', '-require_sw', '0',
                 '-profile:v', 'main', '-pix_fmt', 'yuv420p', '-b:v', '2800k',
                 '-color_range', 'tv', '-colorspace', 'bt709', '-color_trc', 'bt709', '-color_primaries', 'bt709',
                  '-fps_mode', 'passthrough', '-enc_time_base:v', 'filter', '-video_track_timescale', '1000000',
                    '-c:a', 'copy', '-copyinkf:a', '-avoid_negative_ts', 'disabled', '-tag:v', 'hvc1', str(temporary)],
                pass_fds=(lock_fd,) if isinstance(lock_fd, int) else tuple(lock_fd))
        output_info = verify_output(source_info, temporary)
        check_source(item)
        if temporary.stat().st_size >= source.stat().st_size:
            raise ValueError('成品没有变小，保留原片')
        # 硬链接创建不会覆盖其他文件；半途退出的无回执输出不会用于回退。
        os.link(temporary, target)
        saved = {'status': 'archived', 'source': str(source), 'source_stat': item['source_stat'],
                 'output': str(target), 'output_stat': file_stat(target), 'source_info': source_info,
                  'legacy_row': item['legacy_row'],
                   'media_input': str(media_source), 'codec_parameters': codec_parameters,
                   'pre_normalization_source_info': decoded_input,
                   'normalization': 'aspect-preserving-1920x1440' if mixed else 'native',
                   'source_info_scope': 'decoded reference after documented normalization',
                  'output_duration': float(output_info['format']['duration']), 'video_bitrate': 2_800_000,
                  'timestamp_policy': 'source_timestamps_monotonic_1us',
                 'verified_at': datetime.now(timezone.utc).isoformat()}
        atomic_json(receipt_path(source), saved)
        return {'source': str(source), 'status': 'archived', 'saved_bytes': source.stat().st_size - target.stat().st_size}
    finally:
        temporary.unlink(missing_ok=True)


def cleanup(item, *, permanent=False):
    check_source(item)
    source = Path(item['source'])
    saved = archive_receipt(source)
    if saved['source_stat'] != file_stat(source):
        raise ValueError('原片已变化，不能清理')
    source_info = saved['source_info']
    if 'timestamp_max_adjustment_us' not in source_info:
        source_info = probe(source, frames=True)
    verify_output(source_info, archive_path(source))
    check_source(item)
    if permanent:
        source.unlink()
    else:
        command(['trash', str(source)])
    saved['cleanup'] = {'mode': 'deleted' if permanent else 'trashed', 'at': datetime.now(timezone.utc).isoformat()}
    atomic_json(receipt_path(source), saved)
    return {'source': str(source), 'status': saved['cleanup']['mode'],
            'released_bytes': saved['source_stat']['size'] if permanent else 0}


def configure_parser(commands):
    parser = commands.add_parser('archive', help='已完成直播录像归档，默认只读')
    parser.add_argument('action', choices=('inspect', 'plan', 'run', 'verify', 'cleanup'))
    parser.add_argument('--sessions-root', required=True, type=Path)
    parser.add_argument('--session', action='append', default=[])
    parser.add_argument('--max-files', type=int, default=1)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--permanent', action='store_true', help='cleanup时永久删除已验收原片；否则移入Trash')


def main(args):
    if args.max_files < 1 or args.permanent and args.action != 'cleanup':
        raise ValueError('max-files须为正数；permanent仅用于cleanup')
    items = inventory(args.sessions_root, args.session)
    if args.action in ('inspect', 'plan') or args.action in ('run', 'cleanup') and not args.execute:
        print(json.dumps({'read_only': True, 'items': items}, ensure_ascii=False))
        return 0
    results = []
    with writer_lock(args.sessions_root) as lock_fd:
        for item in items:
            if len(results) >= args.max_files:
                break
            eligible = item['action'] == ('encode' if args.action == 'run' else 'archived')
            if not eligible or args.action == 'cleanup' and not Path(item['source']).exists():
                continue
            try:
                if args.action == 'run':
                    result = encode(item, lock_fd)
                elif args.action == 'cleanup':
                    result = cleanup(item, permanent=args.permanent)
                else:
                    saved = archive_receipt(item['source'])
                    verify_output(saved['source_info'], Path(item['output']))
                    result = {'source': item['source'], 'status': 'verified'}
                results.append(result)
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                results.append({'source': item['source'], 'status': 'failed', 'error': str(error)})
    print(json.dumps({'results': results}, ensure_ascii=False))
    return 1 if any(r['status'] == 'failed' for r in results) else 0
