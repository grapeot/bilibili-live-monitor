"""按开播日期呈现完整场次，与既有远程汇总共享数据。"""
import json
import math
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .activity_summary import gift_observations, nonnegative_integer
from .live_share import epoch

FIELDS = ('online_mean', 'online_peak', 'watched_last', 'followers_net', 'amount',
          'amount_nonself', 'paid_donors_nonself')


def summaries(source, sessions):
    rows = []
    for session in sessions:
        lower, upper = epoch(session['start']), epoch(session['end'])
        raw = [(epoch(at), command, packet.get('data') or {}) for at, command, packet in source['raw']
               if lower <= epoch(at) <= upper]
        online = sorted((at, p['online_count']) for at, command, p in raw
                        if command == 'ONLINE_RANK_COUNT' and isinstance(p, dict)
                        and nonnegative_integer(p.get('online_count')) is not None)
        watched = [(at, p['num']) for at, command, p in raw if command == 'WATCHED_CHANGE'
                   and isinstance(p, dict) and nonnegative_integer(p.get('num')) is not None]
        fans = [(at, p['fans']) for at, command, p in raw if command == 'ROOM_REAL_TIME_MESSAGE_UPDATE'
                and isinstance(p, dict) and nonnegative_integer(p.get('fans')) is not None]
        weighted = seconds = 0
        for index, (at, value) in enumerate(online):
            end = min(upper, online[index + 1][0] if index + 1 < len(online) else upper)
            duration = max(0, end - max(lower, at))
            weighted += value * duration
            seconds += duration
        scoped = {**source, 'items': [(kind, at, item) for kind, at, item in source['items']
                                     if lower <= epoch(at) <= upper]}
        gifts = list(gift_observations(scoped))
        observed = bool(raw or scoped['items'] or any(
            lower < epoch(segment['end_utc']) and epoch(segment['start_utc']) < upper
            for segment in source.get('segments', [])))
        amount = None if not observed or any(gift[2] is None for gift in gifts) else round(sum(g[2] for g in gifts) / 1000, 2)
        local = datetime.fromtimestamp(lower, ZoneInfo('Asia/Shanghai'))
        rows.append({'key': session['key'], 'date': local.date().isoformat(),
                     'session': '上午场' if local.hour < 12 else '下午场' if local.hour < 19 else '晚间场',
                     'start_utc': session['start'], 'updated_at': session['end'],
                     'hours': (upper - lower) / 3600, 'status': session['status'],
                     'has_result': session['has_result'],
                     'metrics': {'online_mean': weighted / seconds if seconds else None,
                                 'online_peak': max((v for _, v in online), default=None),
                                 'watched_last': max(watched)[1] if watched else None,
                                 'followers_net': max(fans)[1] - min(fans)[1] if len(fans) >= 2 else None,
                                 'amount': amount, 'amount_nonself': None, 'paid_donors_nonself': None}})
    return sorted(rows, key=lambda row: row['start_utc'], reverse=True)


def shared_summaries(path, local_rows):
    """只投影已发布汇总字段；不输出本机路径或远程私有链接。"""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get('sessions'), list):
        raise ValueError('汇总文件格式无效')
    matches = {row['start_utc']: row for row in local_rows}
    rows = []
    for row in data['sessions']:
        if (not isinstance(row, dict) or not isinstance(row.get('metrics'), dict)
                or not isinstance(row.get('date'), str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', row['date'])
                or not isinstance(row.get('session'), str) or not 0 < len(row['session']) <= 80
                or any(row.get(key) is not None and not isinstance(row[key], str)
                       for key in ('start_utc', 'updated_at'))):
            raise ValueError('场次汇总格式无效')
        metrics = row['metrics']
        values = {key: value if type(value := metrics.get(key)) in (int, float) and math.isfinite(value)
                  else None for key in FIELDS}
        match = matches.get(row.get('start_utc'))
        hours = row.get('hours')
        hours = hours if type(hours) in (int, float) and math.isfinite(hours) and hours >= 0 else None
        status = row.get('status', 'completed')
        status = status if status in ('live', 'finalizing', 'completed') else 'finalizing'
        rows.append({key: row.get(key) for key in ('date', 'session', 'start_utc', 'updated_at')}
                    | {'hours': hours, 'status': status, 'metrics': values,
                       'key': match['key'] if match else None, 'has_result': bool(match and match['has_result'])})
    return rows
