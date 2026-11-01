"""网页同源只读状态与舰队快照；不携带凭证到第三方。"""
import math
from urllib.parse import urlencode

from .rotation import RoomStatusRefused, inspect
from .store import utc_now


async def room_statuses(browser, candidates):
    queries = list(candidates) + [{'url': c['room_id']} for c in candidates if c.get('room_id')]
    return await inspect(await browser.connect(), queries)


async def guard_snapshot(browser, room_id, anchor_uid):
    context = await browser.connect()
    members, unresolved, raw_pages, total, pages = {}, [], [], None, 1
    for page in range(1, 101):
        query = urlencode({'roomid': room_id, 'ruid': anchor_uid, 'page': page, 'page_size': 30})
        response = await context.request.get(
            'https://api.live.bilibili.com/xlive/app-room/v2/guardTab/topList?' + query,
            timeout=20000)
        if response.status != 200:
            error = RoomStatusRefused if response.status in (401, 403, 412, 429) else RuntimeError
            raise error(f'舰队快照HTTP {response.status}')
        payload = await response.json()
        if payload.get('code') != 0:
            raise RoomStatusRefused(f'舰队快照拒绝：code={payload.get("code")}')
        data = payload.get('data', {})
        info = data.get('info', {})
        if not isinstance(data.get('list'), list) or not isinstance(data.get('top3'), list):
            raise RuntimeError('舰队快照结构异常')
        raw_pages.append(payload)
        total = int(info['num'])
        pages = max(1, int(info['page']), math.ceil(total / 30))
        for row in data['top3'] + data['list']:
            uid = str(row.get('uid') or '')
            if uid.isdigit() and int(uid) > 0:
                members[uid] = {**row, 'sender_uid': uid, 'identity_status': 'uid'}
            else:
                unresolved.append({**row, 'sender_uid': None, 'identity_status': 'unresolved'})
        if page >= pages:
            break
    return {'room_id': room_id, 'observed_utc': utc_now(), 'reported_total': total,
            'complete': page >= pages and not unresolved and len(members) == total,
            'members': list(members.values()) + unresolved, 'unresolved_count': len(unresolved), 'raw_pages': raw_pages,
            'source': 'guardTab/topList', 'meaning': '当前榜单快照，不是购买流水或观看名单'}
