"""按开播日期呈现完整场次，与既有远程汇总共享数据。"""
import json
import math
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .archive_metrics import aggregate_metrics
from .time_utils import epoch

FIELDS = ('online_mean', 'online_peak', 'watched_last', 'followers_net', 'amount',
          'amount_nonself', 'paid_donors_nonself')


def summaries(source, sessions):
    rows = []
    for session in sessions:
        lower, upper = epoch(session['start']), epoch(session['end'])
        metrics = aggregate_metrics(source, lower, upper)
        local = datetime.fromtimestamp(lower, ZoneInfo('Asia/Shanghai'))
        rows.append({'key': session['key'], 'date': local.date().isoformat(),
                     'session': '上午场' if local.hour < 12 else '下午场' if local.hour < 19 else '晚间场',
                     'start_utc': session['start'], 'updated_at': session['end'],
                     'hours': (upper - lower) / 3600, 'status': session['status'],
                     'has_result': session['has_result'],
                     'metrics': metrics})
    return sorted(rows, key=lambda row: row['start_utc'], reverse=True)


def shared_summaries(path, local_rows, origin=None):
    """投影已发布汇总字段；历史详情只使用显式配置的HTTPS来源。"""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get('sessions'), list):
        raise ValueError('汇总文件格式无效')
    matches = {row['start_utc']: row for row in local_rows}
    if origin:
        parsed = urlsplit(origin)
        if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('历史详情来源须为HTTPS站点origin')
        origin = origin.rstrip('/')
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
        remote_path = row.get('url')
        detail_url = (origin + remote_path if origin and isinstance(remote_path, str)
                      and re.fullmatch(r'/(?:s|share)/[A-Za-z0-9_-]+/', remote_path) else None)
        hours = row.get('hours')
        hours = hours if type(hours) in (int, float) and math.isfinite(hours) and hours >= 0 else None
        status = row.get('status', 'completed')
        status = status if status in ('live', 'finalizing', 'completed') else 'finalizing'
        rows.append({key: row.get(key) for key in ('date', 'session', 'start_utc', 'updated_at')}
                    | {'hours': hours, 'status': status, 'metrics': values,
                       'key': match['key'] if match else None, 'has_result': bool(match and match['has_result']),
                       'detail_url': detail_url if not match or not match['has_result'] else None})
    return rows
