"""单场欢迎只认可新鲜、明确的开播状态；下播后不重新启动。"""
import asyncio
import json
import time
from datetime import datetime
from urllib.request import urlopen


def live_state(snapshot, room_id, *, now=None):
    now = time.time() if now is None else now
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('policy'), dict):
        return 'paused'
    policy = snapshot.get('policy', {})
    if not isinstance(policy.get('fixed_rooms'), list):
        return 'paused'
    rooms = [str(value).rstrip('/').rsplit('/', 1)[-1] for value in policy.get('fixed_rooms', [])]
    if not policy.get('enabled') or policy.get('mode') != 'fixed' or rooms != [room_id] or policy.get('target_rooms') != 1:
        return 'ended'
    if not isinstance(snapshot.get('candidates'), list):
        return 'paused'
    candidates = [c for c in snapshot['candidates'] if isinstance(c, dict) and c.get('room_id') == room_id]
    if len(candidates) != 1:
        return 'paused'
    candidate = candidates[0]
    try:
        checked = datetime.fromisoformat(candidate['status_checked_utc'])
        if checked.tzinfo is None or not 0 <= now-checked.timestamp() <= 75:
            return 'paused'
    except (ValueError, TypeError, KeyError):
        return 'paused'
    if candidate.get('status_error'):
        return 'paused'
    offline_checks = candidate.get('offline_checks')
    if candidate.get('live_status') in (0, 2) and type(offline_checks) is int and offline_checks >= 2:
        return 'ended'
    if candidate.get('live_status') == 1 and not candidate.get('blocked'):
        return 'live'
    return 'paused'


async def read_live_state(base_url, room_id):
    def read():
        with urlopen(base_url.rstrip('/')+'/api/platform', timeout=10) as response:
            return json.load(response)
    try:
        return live_state(await asyncio.to_thread(read), room_id)
    except (OSError, ValueError):
        return 'paused'
