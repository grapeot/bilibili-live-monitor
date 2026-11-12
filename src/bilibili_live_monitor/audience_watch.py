"""独立的下播后CRM更新适配器；不改采集进程、原场次状态或通知账本。"""
import argparse
import fcntl
import json
import time
from pathlib import Path

from .audience_finalize import advance
from .live_share import atomic_json, epoch
from .live_share_control import load_config


def tick(settings):
    root = Path(settings['workspace']).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        candidates = []
        rooms = set()
        for target in settings['targets']:
            path = Path(target['config']).resolve()
            config = json.loads(path.read_text())
            room = config['room_id']
            if room in rooms:
                raise ValueError('CRM更新目标房间重复')
            rooms.add(room)
            for session_path in sorted(Path(config['sessions_root']).glob('*/config.json')):
                raw = json.loads(session_path.read_text())
                if raw.get('room_id') != room or epoch(raw['start_utc']) < epoch(target['not_before']):
                    continue
                workflow = session_path.parent / 'workflow.json'
                if not workflow.is_file() or json.loads(workflow.read_text()).get('stage') != 'completed':
                    continue
                candidates.append((epoch(raw['start_utc']), session_path, path))
        active_path = root / 'active.json'
        active = json.loads(active_path.read_text()) if active_path.exists() else None
        if active is None:
            for _, session_path, config_path in sorted(candidates):
                session = load_config(session_path)
                key = session['workflow_key']
                receipt = root / (key + '.json')
                if receipt.exists():
                    continue
                active = {'session_config': str(session_path), 'config': str(config_path),
                          'state_file': str(root / (key + '.state.json')),
                          'started_at': time.time(), 'key': key}
                atomic_json(active_path, active)
                break
        if active is None:
            return {'status': 'idle'}
        if active.get('attention'):
            return {'status': 'attention', 'key': active['key'], 'error': active['attention']}
        session = load_config(active['session_config'])
        state_path = Path(active['state_file'])
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        session.update(audience_crm_config=active['config'], audience_state_path=str(state_path))
        try:
            if active.get('last_error'):
                raise ValueError(active['last_error'])
            if time.time() - active['started_at'] > settings.get('timeout_seconds', 21600):
                raise ValueError('CRM更新超过预算')
            complete = advance(session, state)
            atomic_json(state_path, state)
            if complete:
                result = {'status': 'completed', 'key': active['key'], **state['audience_crm']}
                atomic_json(root / (active['key'] + '.json'), result)
                atomic_json(active_path, None)
                return result
            return {'status': 'running', 'key': active['key']}
        except Exception as error:
            # 外部适配器也必须先停止本次CRM会话树，再声明attention。
            from .session_runner import stop_ai
            message = f'{type(error).__name__}: {error}'
            try:
                stopped = stop_ai(session, state.get('audience_session'), children=True)
                from .cursor_finalize import stop_workers
                stopped = stop_workers(session, kinds=('audience_finalize',)) and stopped
            except Exception:
                stopped = False
            atomic_json(state_path, state)
            active['last_error'] = message
            if stopped:
                atomic_json(root / (active['key'] + '.json'),
                            {'status': 'attention', 'key': active['key'], 'error': message,
                             'state_file': str(state_path)})
                atomic_json(active_path, None)
            else:
                atomic_json(active_path, active)
            return {'status': 'attention' if stopped else 'stopping', 'key': active['key'], 'error': message}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(tick(json.loads(args.config.read_text())), ensure_ascii=False))


if __name__ == '__main__':
    main()
