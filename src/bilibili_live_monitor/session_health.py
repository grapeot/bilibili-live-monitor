"""所有活动场次固定五分钟程序巡检；终态停止，不调用内容AI。"""
import argparse
import fcntl
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from .live_share import atomic_json, epoch, iso
from .live_share_control import load_config
from .live_share_watch import request

INTERVAL = 300


def schedule(config, *, delay=INTERVAL):
    label = 'session_health_' + config['workflow_key']
    jobs = request(config['launcher'] + '/scheduled?' + urlencode({'label': label, 'status': 'pending'}))
    for job in jobs:
        if job.get('label') == label:
            return job['id']
    result = request(config['launcher'] + '/run', {
        'command': [sys.executable, '-m', 'bilibili_live_monitor.session_health',
                    '--config', str(Path(config['workspace']) / 'config.json'), '--schedule'],
        'cwd': str(Path(config['workspace'])), 'label': label,
        'delay_seconds': delay, 'timeout': 120, 'diagnose_on_failure': False})
    return result['scheduled_job_id']


def ensure_scheduled(config, *, delay=INTERVAL):
    with (Path(config['workspace']) / 'health.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return schedule(config, delay=delay)


def inspect(config, previous, now):
    state = request(config['api'] + '/api/state')
    platform = request(config['api'] + '/api/platform')
    room = next((r for r in state['rooms'] if r['room_id'] == config['room_id']), {})
    candidate = next((r for r in platform['candidates'] if r['room_id'] == config.get('platform_room_id', config['room_id'])), {})
    with sqlite3.connect(Path(config['database']).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        latest, latest_text, latest_success = db.execute('''SELECT max(json_extract(data,'$.end_utc')),
            max(CASE WHEN length(json_extract(data,'$.text')) > 0
                     AND json_extract(data,'$.is_final') = 1 THEN json_extract(data,'$.end_utc') END),
            max(CASE WHEN json_extract(data,'$.error') IS NULL
                     AND coalesce(json_extract(data,'$.status'), 'done') IN ('done', 'empty')
                     THEN json_extract(data,'$.end_utc') END)
            FROM segments WHERE room_id=? AND json_extract(data,'$.end_utc') >= ?''',
            (config['room_id'], config['start_utc'])).fetchone()
    workspace = Path(config['workspace'])
    workflow = json.loads((workspace / 'workflow.json').read_text())
    snapshot = workspace / 'public' / 'snapshot.json'
    annotations = workspace / 'annotations.json'
    result = {'checked_at': iso(now), 'checked_epoch': now,
              'room_status': room.get('status'), 'recording_status': room.get('recording_status'),
              'recording_bytes': room.get('recording_bytes', 0), 'recorded_seconds': room.get('recorded_seconds', 0),
              'capture_error': room.get('error'), 'asr_ready': state.get('asr_ready'),
              'audio_error': room.get('audio_error'),
              'asr_error': state.get('asr_error'), 'queue_depth': state.get('queue_depth'),
              'latest_segment': latest, 'latest_final_text': latest_text, 'latest_success': latest_success,
              'workflow_stage': workflow.get('stage'), 'workflow_error': workflow.get('last_error'),
              'snapshot_age': now - snapshot.stat().st_mtime if snapshot.exists() else None,
              'review_age': now - annotations.stat().st_mtime if annotations.exists() else None,
              'issues': [], 'recoveries': [t for t in previous.get('recoveries', []) if now - t < 1800]}
    if workflow.get('last_error'):
        result['issues'].append('workflow_error')
    if workflow.get('stage') != 'live':
        return result
    elapsed = now - previous.get('checked_epoch', now)
    stalled = (room.get('record_mode', 'video') != 'none' and elapsed >= 240
               and result['recording_bytes'] <= previous.get('recording_bytes', -1))
    if room.get('status') in ('error', 'stopped') or stalled:
        result['issues'].append('capture_stalled')
    elif room.get('status') == 'reconnecting' or room.get('error'):
        result['issues'].append('capture_reconnecting')
    if room.get('asr_enabled', True):
        if not latest_success or now - epoch(latest_success) > 180:
            result['issues'].append('asr_stale')
        if state.get('asr_error') or room.get('audio_error') or not state.get('asr_ready'):
            result['issues'].append('asr_unhealthy')
    if result['snapshot_age'] is None or result['snapshot_age'] > 180:
        result['issues'].append('publisher_stale')
    if config.get('ai_enabled') and (result['review_age'] is None or result['review_age'] > 900):
        result['issues'].append('review_stale')
    checked = candidate.get('status_checked_utc')
    same_live = (candidate.get('live_status') == 1 and candidate.get('live_start') == config['live_start']
                 and checked and 0 <= now - epoch(checked) <= 180
                 and not candidate.get('blocked') and not candidate.get('blocked_alias'))
    refusal = any(re.search(r'refused|\b(?:403|412|429)\b', str(value), re.I)
                  for value in (room.get('error'), candidate.get('last_error'), candidate.get('status_error'),
                                candidate.get('guard_refused'), platform.get('status_refused')))
    if refusal:
        result['issues'].append('platform_refused')
    if room.get('status') in ('error', 'stopped') and same_live and not refusal and len(result['recoveries']) < 2:
        result['recoveries'].append(now)
        atomic_json(workspace / 'health.json', result)
        try:
            request(config['api'] + '/api/platform/candidates/' + config.get('platform_room_id', config['room_id']) + '/resume', {})
            result['recovery'] = 'room_resume_requested'
        except Exception as error:
            result['recovery_error'] = f'{type(error).__name__}: {error}'[:500]
    return result


def run(config, *, reschedule=False, dry_run=False):
    workspace = Path(config['workspace'])
    with (workspace / 'health.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = workspace / 'health.json'
        previous = json.loads(path.read_text()) if path.exists() else {}
        stage = json.loads((workspace / 'workflow.json').read_text()).get('stage')
        if stage in ('completed', 'attention'):
            result = {'checked_at': iso(time.time()), 'workflow_stage': stage, 'stopped': True}
        elif dry_run:
            result = {'dry_run': True, 'workflow_stage': stage, 'interval_seconds': INTERVAL}
        else:
            try:
                result = inspect(config, previous, time.time())
            except Exception as error:
                result = {**previous, 'checked_at': iso(time.time()),
                          'inspection_error': f'{type(error).__name__}: {error}'[:500]}
            atomic_json(path, result)
            if reschedule:
                try:
                    result['next_job'] = schedule(config)
                except Exception as error:
                    result['schedule_error'] = f'{type(error).__name__}: {error}'[:500]
            with (workspace / 'health_history.jsonl').open('a') as log:
                log.write(json.dumps(result, ensure_ascii=False) + '\n')
        if not dry_run:
            atomic_json(path, result)
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--schedule', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(load_config(args.config), reschedule=args.schedule, dry_run=args.dry_run), ensure_ascii=False))


if __name__ == '__main__':
    main()
