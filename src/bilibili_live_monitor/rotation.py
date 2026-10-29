"""一次性开播巡检；调度器负责调用频率，本模块不启动或重启录制服务。"""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .browser import Browser, room_url


class RoomStatusRefused(RuntimeError):
    """调度器应锁止后续巡检，等待人工检查。"""


def index_rooms(payload):
    if payload.get('code') != 0:
        raise RoomStatusRefused(f'房间状态接口拒绝：code={payload.get("code")}')
    rows = payload.get('data', {}).get('by_room_ids')
    if not isinstance(rows, dict):
        raise RuntimeError('房间状态响应缺少by_room_ids')
    result = {}
    for row in rows.values():
        canonical = str(row.get('room_id', ''))
        if not canonical.isdigit() or canonical == '0':
            continue
        result[canonical] = row
        short = str(row.get('short_id', '0'))
        if short.isdigit() and short != '0':
            result[short] = row
    return result


def plan(candidates, statuses):
    stop, live, unknown, canonical_seen = [], [], [], set()
    blocked_ids = set()
    unresolved_block = False
    for c in candidates:
        if not c.get('blocked'):
            continue
        key = room_url(c['url']).rsplit('/', 1)[-1]
        blocked_ids.add(key)
        if c.get('room_id'):
            blocked_ids.add(str(c['room_id']))
        row = statuses.get(key)
        if row is not None:
            blocked_ids.add(str(row['room_id']))
        elif not c.get('room_id'):
            unresolved_block = True
    # 保留正在使用的短号/长号，避免同一实际房间重复导入。
    for c in sorted(candidates, key=lambda c: not c.get('selected', False)):
        if c.get('blocked'):
            continue
        url = room_url(c['url'])
        row = statuses.get(url.rsplit('/', 1)[-1])
        if row is None or row.get('live_status') not in (0, 1, 2):
            unknown.append(url)
        elif row['live_status'] != 1:
            stop.append(url)
        else:
            canonical = str(row['room_id'])
            # 无法解析既有封禁别名时，不引入新来源；已选来源继续保留。
            if (canonical not in canonical_seen and canonical not in blocked_ids
                    and (not unresolved_block or c.get('selected'))):
                live.append(url)
                canonical_seen.add(canonical)
    return {'stop': stop, 'live': live, 'unknown': unknown}


class Control:
    def __init__(self, base):
        self.base = base.rstrip('/')

    def call(self, path, data=None, method=None):
        request = Request(self.base + '/api/' + path,
                          data=None if data is None else json.dumps(data).encode(),
                          headers={'Content-Type': 'application/json'}, method=method)
        with urlopen(request, timeout=20) as response:
            return json.load(response)


async def inspect(context, candidates):
    ids = list(dict.fromkeys(room_url(c['url']).rsplit('/', 1)[-1] for c in candidates))
    statuses = {}
    for offset in range(0, len(ids), 50):
        query = urlencode([('room_ids', rid) for rid in ids[offset:offset+50]] +
                          [('req_biz', 'web_room_componet'), ('use_cache', 'false')])
        response = await context.request.get(
            'https://api.live.bilibili.com/xlive/web-room/v1/index/getRoomBaseInfo?' + query,
            timeout=20000)
        if response.status != 200:
            raise RoomStatusRefused(f'房间状态接口HTTP {response.status}；不改动现有来源')
        statuses.update(index_rooms(await response.json()))
    return statuses


async def rotate(args):
    control = Control(args.base_url)
    initial = control.call('platform')
    if args.target > initial['capacity']:
        raise ValueError('目标超过现有容量；巡检不重启服务或扩容硬上限')
    if not initial['policy']['enabled']:
        raise ValueError('平台已暂停；巡检不自动恢复')
    if args.target != initial['policy']['target_rooms']:
        raise ValueError('巡检不修改目标；请先显式配置平台policy，再以相同target巡检')
    guard = getattr(args, 'guard', None)
    if guard:
        guard()
    browser = Browser(args.cdp_url)
    try:
        context = await browser.connect()
        candidates = initial['candidates']
        statuses = await inspect(context, candidates)
        result = plan(candidates, statuses)
        discovered = []
        if len(result['live']) < args.target and not initial['discovery_blocked']:
            try:
                discovered = await browser.discover(100)
            except RuntimeError as error:
                if 'discover_refused' in str(error):
                    raise RoomStatusRefused(str(error)) from error
                raise
            known = {c['url'] for c in initial['candidates']}
            discovered = [c for c in discovered if c['url'] not in known]
            statuses.update(await inspect(context, discovered))
            # 对新候选只导入live=1的来源，旧封禁不得通过重新导入解除。
            extra = plan(candidates + discovered, statuses)
            result = extra
        known = {c['url'] for c in candidates}
        result['stop'] = [url for url in result['stop'] if url in known]
        live_set = set(result['live'])
        additions = [c for c in discovered if c['url'] in live_set]
        if args.execute:
            # 二次确认停播；状态缺失或恢复开播均不停止。
            stop_candidates = [{'url': url} for url in result['stop']]
            if stop_candidates:
                await asyncio.sleep(2)
                confirm = await inspect(context, stop_candidates)
                result['stop'] = plan(stop_candidates, confirm)['stop']
            current = control.call('platform')
            if current['policy'] != initial['policy']:
                raise RuntimeError('巡检期间策略改变；不覆盖新策略')
            if current['revision'] != initial['revision'] or not current['policy']['enabled']:
                raise RuntimeError('巡检期间策略改变；中止写入')
            if guard:
                guard()
            if additions:
                control.call('platform/candidates', {'candidates': [
                    {'url': c['url'], 'title': c.get('title', '')[:500], 'priority': 500}
                    for c in additions]})
            # stop和优先级一次性提交，服务端在同一同步操作内检查revision并应用。
            # 导入不递增revision；期间若有人调整目标，此决策返回409，不停止来源。
            if guard:
                guard()
            result['deferred_stop'] = result['stop'][100:]
            result['stop'] = result['stop'][:100]
            control.call('platform/decisions', {'revision': initial['revision'],
                'prioritize': result['live'][:200], 'stop': result['stop'], 'discover': False,
                'reason': '开播巡检：仅停用二次确认live_status为0/2的来源，优先本轮live=1的来源'})
        return {'checked_utc': datetime.now(timezone.utc).isoformat(), 'execute': args.execute,
                'target': args.target, **result, 'imported': [c['url'] for c in additions],
                'before': {k:v for k,v in initial.items() if k not in ('sources', 'candidates')},
                'after': {k:v for k,v in control.call('platform').items() if k not in ('sources', 'candidates')}}
    finally:
        await browser.close()


def configure_parser(commands):
    parser = commands.add_parser('rotate-live', help='检查开播状态并补位；默认dry-run，外部调度器负责周期')
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--cdp-url', required=True)
    parser.add_argument('--target', type=int, required=True, choices=range(1, 201), help='必须与现有policy一致；巡检不修改目标')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path, required=True, help='私有JSON回执路径，不进入公开仓库')


def main(args):
    if not args.output.parent.is_dir():
        raise ValueError('回执父目录不存在；请先建立私有目录')
    result = asyncio.run(rotate(args))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: result[k] for k in ('checked_utc', 'execute', 'target')} | {
        'confirmed_live': len(result['live']), 'stopped': len(result['stop']),
        'unknown': len(result['unknown']), 'added': len(result['imported'])}, ensure_ascii=False))
    return 0
