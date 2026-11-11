import argparse
import asyncio
import json
import os
import sys

from .activity import configure_parser as configure_activity
from .activity import main as activity_main
from .browser import Browser
from .config import load_settings
from .live_share_control import configure_parser as configure_live_share
from .live_share_control import main as live_share_main
from .media_codec import configure_parser as configure_media_codec
from .media_codec import main as media_codec_main
from .offline_asr import configure_parser
from .offline_asr import main as offline_asr_main
from .recording_archive import configure_parser as configure_archive
from .recording_archive import main as archive_main
from .rotation import configure_parser as configure_rotation
from .rotation import main as rotation_main
from .trailer import configure_parser as configure_trailer
from .trailer import main as trailer_main
from .trends import configure_parser as configure_trends
from .trends import main as trends_main


def main():
    parser = argparse.ArgumentParser(description='B站多直播间静音转写')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('serve')
    run = commands.add_parser('run')
    run.add_argument('--until', required=True, help='带时区的绝对截止时间，到时结束所有自管进程')
    commands.add_parser('platform', help='常驻录制平台；目标通过/api/platform管理，SIGTERM优雅关闭')
    discover = commands.add_parser('discover')
    discover.add_argument('--limit', type=int, default=10, choices=range(1, 101))
    configure_parser(commands)
    configure_rotation(commands)
    configure_trends(commands)
    configure_activity(commands)
    configure_trailer(commands)
    configure_live_share(commands)
    configure_archive(commands)
    configure_media_codec(commands)
    args = parser.parse_args()
    if args.command == 'offline-asr':
        return offline_asr_main(args)
    if args.command == 'archive':
        return archive_main(args)
    if args.command == 'media-codec':
        return media_codec_main(args)
    if args.command == 'rotate-live':
        return rotation_main(args)
    if args.command == 'trends':
        return trends_main(args)
    if args.command == 'activity':
        return activity_main(args)
    if args.command == 'trailer':
        return trailer_main(args)
    if args.command == 'live-share':
        return live_share_main(args)
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
    sys.exit(main())
