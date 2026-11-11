"""只读载入单场原始记录并裁剪时间范围，不生成页面或触发任务。"""
import json
import sqlite3
from pathlib import Path

from .time_utils import epoch


def scope_source(source: dict, start: str, end: float) -> dict:
    lower = epoch(start)
    return {
        'raw': [row for row in source['raw'] if lower <= epoch(row[0]) <= end],
        'items': [row for row in source['items'] if lower <= epoch(row[1]) <= end],
        'segments': [s for s in source['segments']
                     if lower <= epoch(s['start_utc']) <= epoch(s['end_utc']) <= end],
    }


def read_source(database: Path, room_id: str, start: str, end: str = '9999') -> dict:
    """读取[start, end)的记录；end为可选的粗上界，调用方仍按精确时间过滤。"""
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        raw = [(at, cmd, json.loads(data)) for at, cmd, data in db.execute(
            'SELECT received_utc,command,data FROM raw_events WHERE room_id=? AND received_utc>=? AND received_utc<? '
            'ORDER BY id', (room_id, start, end))]
        items = [(kind, at, {**json.loads(data), '_record_id': ident}) for ident, kind, at, data in db.execute(
            'SELECT id,kind,at_utc,data FROM timeline WHERE room_id=? AND at_utc>=? AND at_utc<? ORDER BY id',
            (room_id, start, end))]
        segments = [json.loads(data) for data, in db.execute(
            "SELECT data FROM segments WHERE room_id=? AND json_extract(data,'$.start_utc')>=? "
            "AND json_extract(data,'$.start_utc')<? ORDER BY seq", (room_id, start, end))]
    return {'raw': raw, 'items': items, 'segments': segments}
