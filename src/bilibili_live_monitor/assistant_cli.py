"""助手独立启动入口，不启动/关闭 Chrome、ASR 或原后端。"""
import argparse
import asyncio
import fcntl
from dataclasses import fields
from pathlib import Path

import uvicorn

from .assistant import AssistantStore
from .assistant_api import create_sidecar
from .assistant_worker import AssistantWorker, WorkerConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    worker = commands.add_parser('worker')
    control = commands.add_parser('control', help='明确启用/关闭当前场次AI；不启动worker')
    control.add_argument('--db', required=True)
    control.add_argument('--session', required=True)
    switch = control.add_mutually_exclusive_group(required=True)
    switch.add_argument('--enable', action='store_true')
    switch.add_argument('--disable', action='store_true')
    for p in (serve, worker):
        p.add_argument('--db', required=True, **({'action': 'append'} if p is serve else {}))
        p.add_argument('--upstream', default='http://127.0.0.1:8765')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8766)
    serve.add_argument('--static', default='frontend/dist')
    for name in ('source-db', 'room', 'session', 'source-session', 'since'):
        worker.add_argument('--'+name, required=True)
    defaults = WorkerConfig()
    for field in fields(defaults):
        default = getattr(defaults, field.name)
        kind = int if field.name in ('owner_pid', 'max_calls', 'max_events', 'max_bytes', 'max_tokens') else (
            float if default is None or isinstance(default, float) or field.name in ('timeout', 'interval', 'debounce') else type(default))
        worker.add_argument('--'+field.name.replace('_', '-'), default=default, type=kind)
    args = parser.parse_args()
    if args.command == 'control':
        if not Path(args.db).is_file():
            parser.error('db必须是已存在的助手数据库')
        store = AssistantStore(args.db)
        try:
            if (store.get('config') or {}).get('session_id') != args.session:
                parser.error('场次不匹配')
            store.set_enabled(args.enable, reason='explicit_cli')
        finally:
            store.close()
        return
    if args.command == 'serve':
        uvicorn.run(create_sidecar(args.db, args.upstream, args.static), host=args.host, port=args.port)
        return
    if (args.owner_pid is None) != (args.owner_create_time is None):
        parser.error('owner-pid 与 owner-create-time 必须成对提供')
    config = WorkerConfig(**{f.name: getattr(args, f.name) for f in fields(defaults)})
    if min(config.timeout, config.interval, config.max_calls, config.max_events,
           config.max_bytes, config.max_tokens, config.max_tool_turns, config.max_tools_per_turn,
           config.context_tokens, config.recent_seconds, config.tool_result_bytes) <= 0 or config.debounce < 0:
        parser.error('预算、超时和间隔必须为正，debounce 不得为负')
    if Path(args.db).resolve() == Path(args.source_db).resolve():
        parser.error('助手数据库不能是源数据库')
    if not Path(args.source_db).is_file():
        parser.error('source-db 必须是已存在的采集数据库文件')
    store = AssistantStore(args.db)
    try:
        with open(str(store.path)+'.worker.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error('此助手数据库已有 worker')
            store.configure(args.room, args.session, args.since, args.source_session)
            asyncio.run(AssistantWorker(store, args.source_db, args.upstream, config).run())
    finally:
        store.close()


if __name__ == '__main__':
    main()
