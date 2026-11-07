"""私有房间主页的只读聚合；只返回日期与场次指标，不返回原始消息。"""
import json
import math
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .activity_summary import gift_observations, nonnegative_integer
from .session_source import read_source
from .song_followers import merged_windows
from .time_utils import epoch, iso

ZONE = ZoneInfo('Asia/Shanghai')


def within(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError('场次文件超出配置目录')
    return path


def read_json(root, path):
    return json.loads(within(root, path).read_text())


def read_object(root, path):
    value = read_json(root, path)
    if not isinstance(value, dict):
        raise ValueError('场次元数据须为对象')
    return value


def usable_segment(segment):
    return (isinstance(segment, dict) and not segment.get('error') and
            segment.get('status', 'done') in ('done', 'empty', 'silent', 'transcribing'))


def day_windows(start, end):
    while start < end:
        local = datetime.fromtimestamp(start, ZONE)
        midnight = datetime.combine(local.date() + timedelta(days=1), time(), ZONE).timestamp()
        upper = min(end, midnight)
        yield local.date().isoformat(), start, upper
        start = upper


def archive_data(session_root, database, room_id, name, include_summaries=True):
    root = Path(session_root).resolve()
    sessions, windows, coverage, offline_keys = [], [], [], set()
    warnings = 0
    for directory in sorted(root.iterdir()):
        if not re.fullmatch(r'[a-f0-9]{24}', directory.name):
            continue
        try:
            cfg = read_object(root, directory / 'config.json')
            if str(cfg.get('room_id')) != room_id:
                continue
            start = epoch(cfg['start_utc'])
            wf = read_object(root, directory / 'workflow.json')
            snap_path = within(root, directory / 'public/snapshot.json')
            snap = read_object(root, snap_path) if snap_path.is_file() else None
            if snap and epoch(snap['session_start']) != start:
                raise ValueError('场次身份不一致')
            end = epoch(snap['updated_at']) if snap else start
            if not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise ValueError('场次时间无效')
            summary = (snap or {}).get('session_summary', {})
            if not isinstance(summary, dict):
                raise ValueError('场次汇总须为对象')
            row = {'key': directory.name, 'start': iso(start), 'end': iso(end),
                   'status': wf.get('stage', 'created'), 'has_result': snap is not None,
                   'summary': {k: summary.get(k)
                               for k in ('followers_change', 'gift_count', 'gift_amount_yuan', 'watched')},
                   'dates': [d for d, _, _ in day_windows(start, end)] or
                            [datetime.fromtimestamp(start, ZONE).date().isoformat()]}
            offline_path = within(root, directory / 'offline_segments.json')
            staged_coverage = []
            if wf.get('stage') == 'completed' and offline_path.is_file():
                try:
                    offline = read_json(root, offline_path)
                    if not isinstance(offline, list) or not all(isinstance(s, dict) for s in offline):
                        raise ValueError('离线转写须为窗口列表')
                    for segment in offline:
                        a, b = max(start, epoch(segment['start_utc'])), min(end, epoch(segment['end_utc']))
                        if not math.isfinite(a) or not math.isfinite(b):
                            raise ValueError('转写时间无效')
                        if a < b and usable_segment(segment):
                            staged_coverage.append((a, b))
                    offline_keys.add(directory.name)
                except (OSError, ValueError, TypeError, KeyError):
                    # 整份离线文件验证后才纳入，避免部分解析结果抬高覆盖率。
                    staged_coverage = []
                    warnings += 1
            sessions.append(row)
            coverage.extend(staged_coverage)
            if end > start:
                windows.append((start, end))
        except (OSError, ValueError, TypeError, KeyError):
            warnings += 1

    if not sessions:
        return {'room_id': room_id, 'name': name, 'timezone': 'Asia/Shanghai',
                'sessions': [], 'days': [], 'warnings': warnings, 'updated_at': None}
    source = read_source(Path(database), room_id, min(s['start'] for s in sessions))
    from .archive_summary import summaries
    session_summaries = summaries(source, sessions) if include_summaries else []
    windows = merged_windows(windows)

    def observed(at):
        return any(a <= at <= b for a, b in windows)

    # 已完成场次使用离线覆盖，其余场次使用当前转写覆盖；区间合并防止滑窗重复计时。
    live_windows = [(epoch(s['start']), epoch(s['end'])) for s in sessions if s['key'] not in offline_keys]
    for segment in source['segments']:
        if not usable_segment(segment):
            continue
        a, b = epoch(segment['start_utc']), epoch(segment['end_utc'])
        for lo, hi in live_windows:
            left, right = max(a, lo), min(b, hi)
            if left < right:
                coverage.append((left, right))
    coverage = merged_windows(coverage)
    days = {}
    for session in sessions:
        for date in session['dates']:
            days.setdefault(date, {'date': date, 'session_keys': [], 'observed_seconds': 0,
                                  'coverage_seconds': 0, 'danmaku': 0, 'entries': 0,
                                  'gift_count': 0, 'gift_amount_yuan': 0,
                                  'gift_count_unknown': 0, 'gift_amount_unknown': 0,
                                  'followers_change': None, 'followers_partial': False})['session_keys'].append(session['key'])
    for field, spans in [('observed_seconds', windows), ('coverage_seconds', coverage)]:
        for start, end in spans:
            for date, a, b in day_windows(start, end):
                days[date][field] += b - a
    for kind, at, _ in source['items']:
        stamp = epoch(at)
        if observed(stamp) and kind in ('danmaku', 'entry'):
            date = datetime.fromtimestamp(stamp, ZONE).date().isoformat()
            if date in days:
                days[date]['danmaku' if kind == 'danmaku' else 'entries'] += 1
    for stamp, count, amount, _, _ in gift_observations(source):
        date = datetime.fromtimestamp(stamp, ZONE).date().isoformat()
        if observed(stamp) and date in days:
            day = days[date]
            day['gift_count'] += count or 0
            day['gift_count_unknown'] += count is None
            day['gift_amount_yuan'] += (amount or 0) / 1000
            day['gift_amount_unknown'] += amount is None
    points = sorted((epoch(at), packet['data']['fans']) for at, command, packet in source['raw']
                    if command == 'ROOM_REAL_TIME_MESSAGE_UPDATE' and isinstance(packet.get('data'), dict)
                    and nonnegative_integer(packet['data'].get('fans')) is not None)
    watched = sorted((epoch(at), packet['data']['num']) for at, command, packet in source['raw']
                     if command == 'WATCHED_CHANGE' and isinstance(packet.get('data'), dict)
                     and nonnegative_integer(packet['data'].get('num')) is not None)
    # 每个场次分别求观察端点差，不把离线期间的粉丝变化归给下一场。
    for date, day in days.items():
        day_end = datetime.combine(datetime.fromisoformat(date).date() + timedelta(days=1), time(), ZONE).timestamp()
        day['watched_sessions'] = []
        for session in sessions:
            if session['key'] not in day['session_keys']:
                continue
            start, end = epoch(session['start']), epoch(session['end'])
            values = [(at, n) for at, n in watched if start <= at <= end and at < day_end]
            day['watched_sessions'].append({'key': session['key'], 'start': session['start'],
                'value': values[-1][1] if values else None,
                'observed_at': iso(values[-1][0]) if values else None})
        changes = []
        for start, end in windows:
            for d, a, b in day_windows(start, end):
                if d != date:
                    continue
                selected = [(at, n) for at, n in points if a <= at < b]
                prior = [(at, n) for at, n in points if start <= at < a]
                values = (prior[-1:] + selected) if prior else selected
                if len(values) >= 2:
                    changes.append(values[-1][1] - values[0][1])
                    day['followers_partial'] |= not prior and values[0][0] > a
                else:
                    day['followers_partial'] = True
        day['followers_change'] = sum(changes) if changes else None
        day['gift_amount_yuan'] = round(day['gift_amount_yuan'], 3)
        day['has_observations'] = (day['coverage_seconds'] > 0 or any(
            observed(epoch(at)) and datetime.fromtimestamp(epoch(at), ZONE).date().isoformat() == date
            for at, _, _ in source['raw']) or any(
            observed(epoch(at)) and datetime.fromtimestamp(epoch(at), ZONE).date().isoformat() == date
            for _, at, _ in source['items']))
        if not day['has_observations']:
            for field in ('danmaku', 'entries', 'gift_count', 'gift_amount_yuan'):
                day[field] = None
    return {'room_id': room_id, 'name': name, 'timezone': 'Asia/Shanghai',
            'sessions': sorted(sessions, key=lambda s: s['start'], reverse=True),
            'days': sorted(days.values(), key=lambda d: d['date'], reverse=True),
            'summaries': session_summaries, 'summary_source': 'recorded',
            'warnings': warnings, 'updated_at': max(s['end'] for s in sessions)}
