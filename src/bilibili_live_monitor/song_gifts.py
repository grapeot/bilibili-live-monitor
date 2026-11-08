"""按演唱时间窗只读礼物流水；公开聚合不携带账号身份。"""
import json
import sqlite3
import time
from pathlib import Path

from .activity_summary import gift_observations
from .time_utils import epoch, iso


def performance_windows(performance):
    """展示可以合并同歌；礼物仅查询实际内容区间，不延伸到间隙。"""
    segments = performance.get('segments')
    if segments is None:
        return [(performance['start'], performance['end'])]
    return [(epoch(s['start']) if isinstance(s['start'], str) else s['start'],
             epoch(s['end']) if isinstance(s['end'], str) else s['end']) for s in segments]


def summarize(events, windows, coverage, *, inclusive_ends=()):
    selected = [event for event in events if event[0] in inclusive_ends or any(a <= event[0] < b for a, b in windows)]
    return {'gift_count': sum(e[1] or 0 for e in selected),
            'gift_count_unknown': sum(e[1] is None for e in selected),
            'gift_users': len({str(e[3]) for e in selected if e[3]}),
            'gift_users_unknown_events': sum(not e[3] for e in selected),
            'gift_amount_yuan': sum(e[2] or 0 for e in selected) / 1000,
            'gift_amount_unknown': sum(e[2] is None for e in selected),
            'window_complete': all(any(lo <= a and b <= hi for lo, hi in coverage) for a, b in windows)}


def add_gift_stats(data, sessions):
    """同歌跨窗口取并集；跨场相同账号人数去重，不累加每次演唱人数。"""
    sources, session_sources = {}, {}
    for session in sessions:
        source = session.get('gift_source')
        if source:
            source_id = (str(Path(source['database']).resolve()), str(source['room_id']))
            session_sources[str(session['id'])] = source_id
            sources[source_id] = None
    for source_id in sources:
        windows = [w for s in data['songs'] for p in s['performances']
                   if session_sources.get(p['session_id']) == source_id for w in performance_windows(p)]
        if not windows:
            sources[source_id] = ([], [])
            continue
        lower, upper = min(a for a, _ in windows), max(b for _, b in windows)
        database, room = source_id
        with sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            items = [('gift', at, {**json.loads(payload), '_record_id': ident}) for ident, at, payload in db.execute(
                "SELECT id,at_utc,data FROM timeline WHERE room_id=? AND kind='gift' AND at_utc>=? AND at_utc<=? ORDER BY id",
                (room, iso(lower - 1), iso(upper + 1)))]
            points = sorted(epoch(at) for (at,) in db.execute(
                'SELECT received_utc FROM raw_events WHERE room_id=? AND received_utc>=? AND received_utc<=?',
                (room, iso(lower - 121), iso(upper + 121))) if epoch(at) <= time.time())
        coverage = []
        for at in points:
            if coverage and at - coverage[-1][1] <= 120:
                coverage[-1][1] = at
            else:
                coverage.append([at, at])
        sources[source_id] = ([e for e in gift_observations({'items': items}, event_time_only=True)
                               if e[0] is not None], coverage)
    for song in data['songs']:
        by_source = {}
        inclusive_by_source = {}
        for performance in song['performances']:
            source_id = session_sources.get(performance['session_id'])
            if source_id is None:
                performance['gifts'] = None
                continue
            windows = performance_windows(performance)
            inclusive_ends = [performance['end']] if performance.get('include_end') else []
            events, coverage = sources[source_id]
            performance['gifts'] = summarize(events, windows, coverage, inclusive_ends=inclusive_ends)
            performance['gifts'].update(window_start=performance['start'], window_end=performance['end'])
            by_source.setdefault(source_id, []).extend(windows)
            inclusive_by_source.setdefault(source_id, []).extend(inclusive_ends)
        if len(by_source) != 1 or any(p['gifts'] is None for p in song['performances']):
            song['gifts'] = None
        else:
            source_id, windows = next(iter(by_source.items()))
            song['gifts'] = summarize(sources[source_id][0], windows, sources[source_id][1],
                                      inclusive_ends=inclusive_by_source[source_id])


def render_gifts(stats):
    if stats is None:
        return '<p class="muted song-gifts">礼物统计暂无数据</p>'
    count = str(stats['gift_count']) + ('+' if stats['gift_count_unknown'] else '')
    users = str(stats['gift_users']) + ('+' if stats['gift_users_unknown_events'] else '')
    amount = f'{stats["gift_amount_yuan"]:,.2f}' + ('+' if stats['gift_amount_unknown'] else '')
    note = '' if stats['window_complete'] else ' · 统计窗口未完整覆盖'
    return f'<p class="song-gifts">片段内礼物 {count} 件 · 送礼 {users} 人 · ¥{amount}{note}</p>'
