"""整场终审混合时间线，只保存私有数据库，不生成公开页面。"""
import hashlib
import json
import sqlite3
from pathlib import Path

from .live_share import epoch, fingerprint
from .live_share_control import source_at_cutoff
from .transcript_corrections import correction_context


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def build(config, cutoff):
    _, bound, source = source_at_cutoff(config)
    if bound != cutoff:
        raise ValueError('最终时间线截止点不匹配')
    if config.get('talk_finalize'):
        from .talk_finalize import complete
        if not complete(config):
            raise ValueError('杂谈精修尚未完整入库')
    corrections, _ = correction_context(Path(config['workspace']), source)
    rows = []
    for segment in source['segments']:
        if not segment.get('is_final') or not segment.get('text'):
            continue
        current = {**segment, **corrections.get(segment['seq'], {})}
        rows.append({'kind': 'transcript', 'at_utc': segment['start_utc'], 'end_utc': segment['end_utc'],
                     'seq': segment['seq'], 'fingerprint': fingerprint(segment),
                     'original_text': segment['text'], 'text': current.get('corrected_text', segment['text']),
                     'correction_id': current.get('correction_id'),
                     'correction_reason': current.get('correction_reason'),
                     'correction_evidence': current.get('correction_evidence', [])})
    for kind, at, data in source['items']:
        if kind != 'transcript':
            # 保留原始结构，包括弹幕昵称、送礼人、礼物数量/金额和来源ID。
            rows.append({'kind': kind, 'at_utc': at, 'data': data})
    rows.sort(key=lambda row: (epoch(row['at_utc']), row['kind'],
                              str(row.get('seq', row.get('data', {}).get('_record_id', '')))))
    return rows


def commit(config, cutoff):
    rows = build(config, cutoff)
    identity = {'workflow_key': config['workflow_key'], 'room_id': config['room_id'],
                'start_utc': config['start_utc'], 'cutoff': float(cutoff)}
    version = digest({**identity, 'rows': rows})
    with sqlite3.connect(config['database'], timeout=30) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS finalized_timeline_versions(
            workflow_key TEXT NOT NULL, version TEXT NOT NULL, room_id TEXT NOT NULL,
            start_utc TEXT NOT NULL, cutoff REAL NOT NULL, row_count INTEGER NOT NULL,
            PRIMARY KEY(workflow_key,version))''')
        db.execute('''CREATE TABLE IF NOT EXISTS finalized_timeline(
            workflow_key TEXT NOT NULL, version TEXT NOT NULL, position INTEGER NOT NULL,
            kind TEXT NOT NULL, at_utc TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY(workflow_key,version,position))''')
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT version FROM finalized_timeline_versions WHERE workflow_key=?',
                              (config['workflow_key'],)).fetchall()
        if previous and previous != [(version,)]:
            raise ValueError('本场已有不同最终时间线，不自动改写历史版本')
        if previous:
            saved = [json.loads(raw) for raw, in db.execute(
                'SELECT data FROM finalized_timeline WHERE workflow_key=? AND version=? ORDER BY position',
                (config['workflow_key'], version))]
            if saved != rows:
                raise ValueError('最终时间线数据库内容已变化')
        else:
            db.executemany('INSERT INTO finalized_timeline VALUES(?,?,?,?,?,?)', [
                (config['workflow_key'], version, position, row['kind'], row['at_utc'],
                 json.dumps(row, ensure_ascii=False, sort_keys=True)) for position, row in enumerate(rows)])
            db.execute('INSERT INTO finalized_timeline_versions VALUES(?,?,?,?,?,?)',
                       (config['workflow_key'], version, config['room_id'], config['start_utc'], cutoff, len(rows)))
    return {'version': version, 'rows': len(rows)}


def verify(config, cutoff):
    with sqlite3.connect(Path(config['database']).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        version = db.execute('SELECT version,room_id,start_utc,cutoff,row_count FROM finalized_timeline_versions '
                             'WHERE workflow_key=?', (config['workflow_key'],)).fetchall()
        if len(version) != 1 or version[0][1:4] != (config['room_id'], config['start_utc'], cutoff):
            raise ValueError('最终时间线场次身份或截止点不匹配')
        version, room, start, bound, count = version[0]
        rows = [json.loads(raw) for raw, in db.execute(
            'SELECT data FROM finalized_timeline WHERE workflow_key=? AND version=? ORDER BY position',
            (config['workflow_key'], version))]
    if len(rows) != count or digest({'workflow_key': config['workflow_key'], 'room_id': room,
                                     'start_utc': start, 'cutoff': bound, 'rows': rows}) != version:
        raise ValueError('最终时间线数据库完整性校验失败')
    return {'version': version, 'rows': count}
