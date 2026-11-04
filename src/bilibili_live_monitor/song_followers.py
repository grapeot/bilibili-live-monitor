"""歌曲窗口内关注总数的净变化；不推断新增账号或歌曲因果。"""
import json
import sqlite3
from bisect import bisect_left, bisect_right
from pathlib import Path

from .activity_summary import nonnegative_integer
from .live_share import epoch, iso


def merged_windows(windows):
    result = []
    for start, end in sorted(windows):
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def net_change(points, windows, coverage):
    """左端取截至该刻的值，右端取严格早于该刻的值；缺测不当零。"""
    times = [at for at, _ in points]
    total = 0
    for start, end in merged_windows(windows):
        left, right = bisect_right(times, start) - 1, bisect_left(times, end) - 1
        if left < 0 or right < left:
            return None
        # Fan counters are change notifications, not periodic samples. Carry the
        # last value only while the raw-message stream covers the whole window
        # and its baseline, using the same 120s gap rule as gift coverage.
        if not any(lo <= times[left] <= start and end <= hi for lo, hi in coverage):
            return None
        total += points[right][1] - points[left][1]
    return total


def add_follower_stats(data, sessions):
    session_sources, sources = {}, {}
    for session in sessions:
        source = session.get('gift_source')
        if source:
            ident = (str(Path(source['database']).resolve()), str(source['room_id']))
            session_sources[str(session['id'])] = ident
            sources[ident] = ([], [])
    for ident in sources:
        windows = [(p['start'] - 30, p['end'] + 120) for song in data['songs'] for p in song['performances']
                   if session_sources.get(p['session_id']) == ident]
        if not windows:
            continue
        lower, upper = min(a for a, _ in windows), max(b for _, b in windows)
        database, room = ident
        with sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            previous = db.execute('SELECT received_utc FROM raw_events WHERE room_id=? AND command=? '
                                  'AND received_utc<=? AND json_type(data,\'$.data.fans\')=\'integer\' '
                                  'AND json_extract(data,\'$.data.fans\')>=0 ORDER BY received_utc DESC,id DESC LIMIT 1',
                                  (room, 'ROOM_REAL_TIME_MESSAGE_UPDATE', iso(lower))).fetchone()
            baseline = epoch(previous[0]) if previous else lower
            rows = db.execute('SELECT received_utc,data FROM raw_events WHERE room_id=? AND command=? '
                              'AND received_utc>=? AND received_utc<=? ORDER BY received_utc,id',
                              (room, 'ROOM_REAL_TIME_MESSAGE_UPDATE', iso(baseline - 1), iso(upper + 1)))
            points = {}
            for at, payload in rows:
                body = json.loads(payload).get('data')
                value = body.get('fans') if isinstance(body, dict) else None
                if nonnegative_integer(value) is not None:
                    points[epoch(at)] = value
            coverage = []
            for (at,) in db.execute('SELECT received_utc FROM raw_events WHERE room_id=? '
                                    'AND received_utc>=? AND received_utc<=? ORDER BY received_utc,id',
                                    (room, iso(baseline - 121), iso(upper + 121))):
                at = epoch(at)
                if coverage and at - coverage[-1][1] <= 120:
                    coverage[-1][1] = at
                else:
                    coverage.append([at, at])
            sources[ident] = (sorted(points.items()), coverage)
    for song in data['songs']:
        grouped = {}
        for performance in song['performances']:
            ident = session_sources.get(performance['session_id'])
            window = (performance['start'] - 30, performance['end'] + 120)
            performance['followers_change'] = net_change(sources[ident][0], [window], sources[ident][1]) if ident else None
            if ident:
                grouped.setdefault(ident, []).append(window)
        song['followers_change'] = None
        if len(grouped) == 1 and all(p['followers_change'] is not None for p in song['performances']):
            ident, windows = next(iter(grouped.items()))
            song['followers_change'] = net_change(sources[ident][0], windows, sources[ident][1])


def render_followers(change):
    if change is None:
        return '<p class="song-followers muted">净增关注暂无数据（窗口观测不足）</p>'
    value = f'{change:+d}' if change else '0'
    return f'<p class="song-followers">净增关注 {value} 人</p>'
