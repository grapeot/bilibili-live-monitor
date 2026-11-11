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


def history_cutoffs(history_sources):
    """跨实例交接前的数据来源；每项含database、session_root及交接截止until，按截止排序后各取相邻截止之间的区间。"""
    result = []
    for item in history_sources or ():
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ('database', 'session_root', 'until')):
            raise ValueError('历史来源须含database、session_root与until')
        until = epoch(item['until'])
        if not math.isfinite(until):
            raise ValueError('历史来源截止时间无效')
        result.append({**item, 'until_epoch': until})
    result.sort(key=lambda item: item['until_epoch'])
    for index, item in enumerate(result):
        item['since_epoch'] = result[index - 1]['until_epoch'] if index else -math.inf
    return result


def merge_sources(main, parts, cutoff):
    """每段只取自身区间，当前来源只取最后截止之后，避免交接边界重复计数。

    各段按时间先后拼接，不重新排序，保留各库内原有的记录顺序。"""
    def keep(source, lower, upper):
        return {'raw': [row for row in source['raw'] if lower <= epoch(row[0]) < upper],
                'items': [row for row in source['items'] if lower <= epoch(row[1]) < upper],
                'segments': [s for s in source['segments'] if lower <= epoch(s['start_utc']) < upper]}
    merged = {'raw': [], 'items': [], 'segments': []}
    for part in [keep(source, lower, upper) for source, lower, upper in parts] + [keep(main, cutoff, math.inf)]:
        for key in merged:
            merged[key].extend(part[key])
    return merged


def archive_data(session_root, database, room_id, name, include_summaries=True, *, gift_share=None, now=None,
                 historical_windows=(), historical_root=None, history_sources=()):
    root = Path(session_root).resolve()
    sessions, windows, coverage, offline_keys = [], [], [], set()
    warnings = 0
    share_enabled = isinstance(gift_share, dict) and gift_share.get('enabled') is True
    share_uids = set(str(uid) for uid in gift_share.get('uids', []) if str(uid).isdigit()) if share_enabled else set()
    share_label = str(gift_share.get('label') or '指定观众')[:80] if share_enabled and share_uids else None
    sources = history_cutoffs(history_sources)
    roots = [root]
    if historical_root and Path(historical_root).resolve() != root:
        roots.append(Path(historical_root).resolve())
    roots = [(r, None) for r in roots] + [(Path(item['session_root']).resolve(), item) for item in sources]
    seen_keys = set()
    for root, bounds, directory in [(r, b, d) for r, b in roots for d in sorted(r.iterdir())]:
        if not re.fullmatch(r'[a-f0-9]{24}', directory.name):
            continue
        if directory.name in seen_keys:
            continue
        try:
            cfg = read_object(root, directory / 'config.json')
            if str(cfg.get('room_id')) != room_id:
                continue
            start = epoch(cfg['start_utc'])
            if bounds and not bounds['since_epoch'] <= start < bounds['until_epoch']:
                continue
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
            seen_keys.add(directory.name)
            coverage.extend(staged_coverage)
            if end > start:
                windows.append((start, end))
        except (OSError, ValueError, TypeError, KeyError):
            warnings += 1

    history = []
    for window in historical_windows:
        start, end = epoch(window['start']), epoch(window['end'])
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError('历史统计时间无效')
        history.append((start, end))
    windows.extend(history)
    if not sessions and not windows:
        from .period_overview import period_overview
        result = {'room_id': room_id, 'name': name, 'timezone': 'Asia/Shanghai',
                'sessions': [], 'days': [], 'periods': period_overview([], now=now, share_label=share_label),
                'warnings': warnings, 'updated_at': None}
        if share_label is not None:
            result['gift_share'] = {'label': share_label, 'percentage': None}
        return result
    earliest = min([a for a, _ in windows] + [epoch(s['start']) for s in sessions])
    source = read_source(Path(database), room_id, iso(earliest))
    if sources:
        # 历史库只读到截止前（多读一秒，再按精确边界过滤）。
        source = merge_sources(source, [(read_source(Path(item['database']), room_id, iso(earliest),
                                                     iso(item['until_epoch'] + 1)),
                                         item['since_epoch'], item['until_epoch']) for item in sources],
                               sources[-1]['until_epoch'])
    if history:
        # 早期归档可能只有原始进房报文；仅补足已授权历史窗口的缺失事件。
        from .danmaku import entry_message
        seen = {item.get('source_key') for kind, _, item in source['items'] if kind == 'entry'}
        for at, command, packet in source['raw']:
            if command not in ('INTERACT_WORD', 'INTERACT_WORD_V2'):
                continue
            if not any(a <= epoch(at) <= b for a, b in history):
                continue
            try:
                entry = entry_message({**packet, 'cmd': command}, received_utc=at)
            except (ValueError, TypeError, KeyError):
                warnings += 1
                continue
            if entry and entry['source_key'] not in seen:
                source['items'].append(('entry', at, entry))
                seen.add(entry['source_key'])
    from .archive_summary import summaries
    session_summaries = summaries(source, sessions) if include_summaries else []
    windows = merged_windows(windows)

    def observed(at):
        return any(a <= at <= b for a, b in windows)

    # 已完成场次使用离线覆盖，其余场次使用当前转写覆盖；区间合并防止滑窗重复计时。
    live_windows = [(epoch(s['start']), epoch(s['end'])) for s in sessions if s['key'] not in offline_keys] + history
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
    for date in {date for session in sessions for date in session['dates']} | {
            date for start, end in history for date, _, _ in day_windows(start, end)}:
        days[date] = {'date': date, 'session_keys': [], 'observed_seconds': 0,
                                   'coverage_seconds': 0, 'danmaku': 0, 'entries': 0,
                                   'gift_count': 0, 'gift_amount_yuan': 0,
                                   'gift_count_unknown': 0, 'gift_amount_unknown': 0,
                                   'followers_change': None, 'followers_partial': False}
    for session in sessions:
        for date in session['dates']:
            days[date]['session_keys'].append(session['key'])
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
    share_amounts = {}
    for stamp, count, amount, uid, _ in gift_observations(source, platform_uid=True):
        date = datetime.fromtimestamp(stamp, ZONE).date().isoformat()
        if observed(stamp) and date in days:
            day = days[date]
            day['gift_count'] += count or 0
            day['gift_count_unknown'] += count is None
            day['gift_amount_yuan'] += (amount or 0) / 1000
            day['gift_amount_unknown'] += amount is None
            if share_label is not None and amount is not None:
                if uid is None and amount > 0:
                    share_amounts[date] = None
                elif str(uid) in share_uids and share_amounts.get(date, 0) is not None:
                    share_amounts[date] = share_amounts.get(date, 0) + amount / 1000
    points = sorted((epoch(at), packet['data']['fans']) for at, command, packet in source['raw']
                    if command == 'ROOM_REAL_TIME_MESSAGE_UPDATE' and isinstance(packet.get('data'), dict)
                    and nonnegative_integer(packet['data'].get('fans')) is not None)
    # 粉丝团为平台推送的当前点亮人数（存量），只在开播期间推送；每天取最后一次推送作为收盘值。
    clubs = sorted((epoch(at), packet['data']['fans_club']) for at, command, packet in source['raw']
                   if command == 'ROOM_REAL_TIME_MESSAGE_UPDATE' and isinstance(packet.get('data'), dict)
                   and nonnegative_integer(packet['data'].get('fans_club')) is not None and observed(epoch(at)))
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
        day['has_message_observations'] = (any(
            observed(epoch(at)) and datetime.fromtimestamp(epoch(at), ZONE).date().isoformat() == date
            for at, _, _ in source['raw']) or any(
            observed(epoch(at)) and datetime.fromtimestamp(epoch(at), ZONE).date().isoformat() == date
            for _, at, _ in source['items']))
        day['has_observations'] = day['coverage_seconds'] > 0 or day['has_message_observations']
        if not day['has_message_observations']:
            for field in ('danmaku', 'entries', 'gift_count', 'gift_amount_yuan'):
                day[field] = None
    closes = {}
    for at, value in clubs:
        closes[datetime.fromtimestamp(at, ZONE).date().isoformat()] = (at, value)
    previous = None
    for date in sorted(days):
        day = days[date]
        close = closes.get(date)
        day['fans_club_close'] = close[1] if close else None
        day['fans_club_close_at'] = iso(close[0]) if close else None
        # 与上一个有推送的日期比较，跨过下播期间，到期熄灭也计入变化。
        day['fans_club_change'] = close[1] - previous[1] if close and previous else None
        day['fans_club_change_since'] = previous[0] if close and previous else None
        if close:
            previous = (date, close[1])
    from .period_overview import overall_share, period_overview
    result = {'room_id': room_id, 'name': name, 'timezone': 'Asia/Shanghai',
            'sessions': sorted(sessions, key=lambda s: s['start'], reverse=True),
            'days': sorted(days.values(), key=lambda d: d['date'], reverse=True),
            'summaries': session_summaries, 'summary_source': 'recorded',
             'warnings': warnings, 'updated_at': iso(max([b for _, b in windows] +
                                                         [epoch(s['end']) for s in sessions]))}
    result['periods'] = period_overview(result['days'], now=now, share_amounts=share_amounts, share_label=share_label)
    if share_label is not None:
        result['gift_share'] = overall_share(result['days'], share_amounts, share_label)
    return result
