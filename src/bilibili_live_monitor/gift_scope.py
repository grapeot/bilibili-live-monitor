"""礼物尾段范围仅来自冻结录像清单，不修改生产场次截止时间。"""
import json
import math

from .gift_media import session_config


def recorded_tail(session):
    root, _ = session_config(session)
    manifest = json.loads((root/'offline/manifest.json').read_text())
    cutoff = float(manifest['cutoff'])
    if not math.isfinite(cutoff):
        raise ValueError('场次截止时间无效')
    rows = sorted(manifest.get('rows', []), key=lambda r: r['wall_start'])
    # cutoff交界属于截止前的录制，不能把恰在cutoff开始的新run认作旧场尾段。
    anchors = [r for r in rows if r['wall_start'] < cutoff <= r['wall_start']+r['duration_sec']]
    result = {'cutoff': cutoff, 'recorded_end': cutoff, 'stop_reason': 'no_recording_at_cutoff'}
    if len(anchors) != 1:
        if anchors:
            result['stop_reason'] = 'ambiguous_recording_at_cutoff'
        return result
    anchor = anchors[0]
    end = max(cutoff, anchor['wall_start']+anchor['duration_sec'])
    identity = tuple(anchor.get(k) for k in ('run_group', 'run_start'))
    reason = 'recording_end'
    for row in rows[rows.index(anchor)+1:]:
        if abs(row['wall_start']-end) > 1e-6:
            if row['wall_start'] < end:
                end = max(cutoff, row['wall_start'])
            reason = 'recording_gap_or_overlap'
            break
        if tuple(row.get(k) for k in ('run_group', 'run_start')) != identity:
            reason = 'different_recording_run'
            break
        end = row['wall_start']+row['duration_sec']
    if not math.isfinite(end) or end < cutoff:
        raise ValueError('录像尾段时间无效')
    return {**result, 'recorded_end': end, 'stop_reason': reason}


def event_cutoff(session, include_recorded_tail=False):
    scope = recorded_tail(session)
    return scope['recorded_end'] if include_recorded_tail else scope['cutoff']
