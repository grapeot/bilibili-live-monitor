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
parser.add_argument('--state-file', type=Path, required=True)
parser.add_argument('--execute', action='store_true')
parser.add_argument('--minimum', type=int, default=4)
parser.add_argument('--window', type=float, default=30)
parser.add_argument('--cooldown', type=float, default=300)
parser.add_argument('--wait-seconds', type=float, default=600)
args = parser.parse_args()
result = asyncio.run(run_once(args.database, args.room_id, args.cdp_url, args.state_file,
                             execute=args.execute, minimum=args.minimum, window=args.window,
                             cooldown=args.cooldown, wait_seconds=args.wait_seconds))
print(json.dumps(result, ensure_ascii=False))
