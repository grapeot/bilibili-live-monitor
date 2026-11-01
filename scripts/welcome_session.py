"""显式启动单场持续欢迎；任务结束/失败后不得自动重新执行。"""
import argparse
import asyncio
import json
from pathlib import Path

from bilibili_live_monitor.welcome import run_welcome
from bilibili_live_monitor.welcome_session import read_live_state

parser = argparse.ArgumentParser()
parser.add_argument('--database', type=Path, required=True)
parser.add_argument('--room-id', required=True)
parser.add_argument('--cdp-url', required=True)
parser.add_argument('--api-url', required=True)
parser.add_argument('--since', required=True)
parser.add_argument('--execute', action='store_true')
parser.add_argument('--minimum', type=int, default=2)
parser.add_argument('--window', type=float, default=30)
parser.add_argument('--cooldown', type=float, default=300)
parser.add_argument('--last-sent-at', type=float)
args = parser.parse_args()


async def main():
    return await run_welcome(args.database, args.room_id, args.cdp_url,
                             args.database.parent/f'welcome_session_{args.room_id}.json',
                             since=args.since, execute=args.execute, continuous=True,
                             minimum=args.minimum, window=args.window, cooldown=args.cooldown,
                             last_sent=args.last_sent_at,
                             live_check=lambda: read_live_state(args.api_url, args.room_id))


print(json.dumps(asyncio.run(main()), ensure_ascii=False))
