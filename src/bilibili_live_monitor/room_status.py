"""网页同源只读状态与舰队快照；不携带凭证到第三方。"""
from urllib.parse import urlencode

from .rotation import RoomStatusRefused, inspect
from .store import utc_now


async def room_statuses(browser, candidates):
    return await inspect(await browser.connect(), candidates)


async def guard_snapshot(browser, room_id, anchor_uid):
    context = await browser.connect()
    members, raw_pages, total, pages = {}, [], None, 1
    for page in range(1, 101):
        query = urlencode({'roomid': room_id, 'ruid': anchor_uid, 'page': page, 'page_size': 30})
        response = await context.request.get(
            'https://api.live.bilibili.com/xlive/app-room/v2/guardTab/topList?' + query,
            timeout=20000)
        if response.status != 200:
            raise RoomStatusRefused(f'舰队快照HTTP {response.status}')
        payload = await response.json()
        if payload.get('code') != 0:
            raise RoomStatusRefused(f'舰队快照拒绝：code={payload.get("code")}')
        data = payload.get('data', {})
        info = data.get('info', {})
        if not isinstance(data.get('list'), list) or not isinstance(data.get('top3'), list):
            raise RuntimeError('舰队快照结构异常')
        raw_pages.append(payload)
        total = int(info['num'])
        pages = max(1, int(info['page']))
        for row in data['top3'] + data['list']:
            uid = str(row.get('uid') or '')
            if uid.isdigit() and int(uid) > 0:
                members[uid] = row
        if page >= pages:
            break
    return {'room_id': room_id, 'observed_utc': utc_now(), 'reported_total': total,
            'complete': page >= pages and len(members) == total,
            'members': list(members.values()), 'raw_pages': raw_pages,
            'source': 'guardTab/topList', 'meaning': '当前榜单快照，不是购买流水或观看名单'}
