import argparse
import asyncio
import json
import os

from .browser import Browser
from .config import load_settings


def main():
    parser = argparse.ArgumentParser(description='B站多直播间静音转写')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('serve')
    run = commands.add_parser('run')
    run.add_argument('--until', required=True, help='带时区的绝对截止时间，到时结束所有自管进程')
    commands.add_parser('platform', help='常驻录制平台；目标通过/api/platform管理，SIGTERM优雅关闭')
    discover = commands.add_parser('discover')
    discover.add_argument('--limit', type=int, default=10, choices=range(1, 101))
    args = parser.parse_args()
    settings = load_settings()
    if args.command == 'serve':
        import uvicorn

        from .app import create_app
        host = os.getenv('APP_HOST', '127.0.0.1')
        if host not in ('127.0.0.1', 'localhost', '::1'):
            parser.error('当前版本仅支持回环地址监听')
        uvicorn.run(create_app(settings), host=host, port=int(os.getenv('APP_PORT', '8765')),
                    timeout_graceful_shutdown=5)
    elif args.command in ('run', 'platform'):
        from .runtime import run_session
        run_session(settings, args.until if args.command == 'run' else None)
    else:
        async def run():
            browser = Browser(settings.cdp_url)
            try:
                print(json.dumps(await browser.discover(args.limit), ensure_ascii=False, indent=2))
            finally:
                await browser.close()
        asyncio.run(run())


if __name__ == '__main__':
    main()
