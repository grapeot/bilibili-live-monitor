"""一次性欢迎测试，默认dry-run；--execute才会点击发送。"""
import argparse
import asyncio
import json
from pathlib import Path

from bilibili_live_monitor.welcome import run_once

parser = argparse.ArgumentParser()
parser.add_argument('--database', type=Path, required=True)
parser.add_argument('--room-id', required=True)
parser.add_argument('--cdp-url', required=True)
parser.add_argument('--since', required=True, help='本场观察起点ISO8601，必须带时区')
parser.add_argument('--execute', action='store_true')
parser.add_argument('--minimum', type=int, default=4)
parser.add_argument('--window', type=float, default=30)
parser.add_argument('--cooldown', type=float, default=300)
parser.add_argument('--wait-seconds', type=float, default=600)
args = parser.parse_args()
state_file = args.database.parent/f'welcome_once_{args.room_id}.json'
result = asyncio.run(run_once(args.database, args.room_id, args.cdp_url, state_file, since=args.since,
                             execute=args.execute, minimum=args.minimum, window=args.window,
                             cooldown=args.cooldown, wait_seconds=args.wait_seconds))
print(json.dumps(result, ensure_ascii=False))
