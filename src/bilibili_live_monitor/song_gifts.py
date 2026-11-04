"""按演唱时间窗只读礼物流水；公开聚合不携带账号身份。"""
import json
import sqlite3
import time
from pathlib import Path

from .activity_summary import gift_observations
from .live_share import epoch, iso


def summarize(events, windows, coverage):
    selected = [event for event in events if any(a <= event[0] < b for a, b in windows)]
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
        windows = [(p['start'] - 30, p['end'] + 120) for s in data['songs'] for p in s['performances']
                   if session_sources.get(p['session_id']) == source_id]
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
        sources[source_id] = (gift_observations({'items': items}), coverage)
    for song in data['songs']:
        by_source = {}
        for performance in song['performances']:
            source_id = session_sources.get(performance['session_id'])
            if source_id is None:
                performance['gifts'] = None
                continue
            window = (performance['start'] - 30, performance['end'] + 120)
            events, coverage = sources[source_id]
            performance['gifts'] = summarize(events, [window], coverage)
            performance['gifts'].update(window_start=window[0], window_end=window[1])
            by_source.setdefault(source_id, []).append(window)
        if len(by_source) != 1 or any(p['gifts'] is None for p in song['performances']):
            song['gifts'] = None
        else:
            source_id, windows = next(iter(by_source.items()))
            song['gifts'] = summarize(sources[source_id][0], windows, sources[source_id][1])


def render_gifts(stats):
    if stats is None:
        return '<p class="muted song-gifts">礼物统计暂无数据</p>'
    count = str(stats['gift_count']) + ('+' if stats['gift_count_unknown'] else '')
    users = str(stats['gift_users']) + ('+' if stats['gift_users_unknown_events'] else '')
    amount = f'{stats["gift_amount_yuan"]:,.2f}' + ('+' if stats['gift_amount_unknown'] else '')
    note = '' if stats['window_complete'] else ' · 统计窗口未完整覆盖'
    return f'<p class="song-gifts">礼物 {count} 件 · 送礼 {users} 人 · ¥{amount}{note}</p>'
