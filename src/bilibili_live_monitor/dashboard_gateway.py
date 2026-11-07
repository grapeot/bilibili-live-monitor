"""同源多实例看板；仅代理明确配置的本机监控服务，不共享Chrome或账号。"""
import argparse
import asyncio
import html
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


def validate_instances(instances):
    result = {}
    for row in instances:
        ident, url = row['id'], row['url'].rstrip('/')
        parsed = urlsplit(url)
        if (not re.fullmatch(r'[a-z0-9_-]+', ident) or ident in result
                or parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('实例须为唯一ID及本机HTTP服务地址')
        result[ident] = {**row, 'url': url}
    if not result:
        raise ValueError('至少配置一个监控实例')
    return result


def browser_automation(data):
    """只发送控制界面所需字段，工作目录与发布目标留在服务器。"""
    def project(value, keys):
        return {key: value[key] for key in keys if key in value}

    account = project(data.get('account', {}), ('mode', 'managed', 'active', 'logged_in', 'name', 'switching'))
    account['profiles'] = [project(p, ('id', 'label')) for p in data.get('account', {}).get('profiles', [])]
    return {'account': account,
            'rooms': [{**project(r, ('room_id', 'label', 'intro', 'enabled', 'confirmed', 'delivery')),
                       **({'error': '自动化执行异常，请检查本机日志'} if r.get('error') else {})}
                      for r in data.get('rooms', [])],
            'likes': [project(r, ('id', 'room_id', 'requested', 'triggered', 'status')) for r in data.get('likes', [])],
            'notifications': [project(r, ('id', 'label', 'event', 'status')) for r in data.get('notifications', [])]}


def room_snapshot(state, platform, automation, instance):
    account = automation.get('account', {})
    mode = account.get('mode', 'account')
    label = '匿名' if mode == 'anonymous' else next(
        (p['label'] for p in account.get('profiles', []) if p['id'] == account.get('active')), instance['label'])
    rooms = {r['room_id']: dict(r) for r in state.get('rooms', [])}
    fixed = platform.get('policy', {}).get('fixed_rooms', [])
    for candidate in platform.get('candidates', []):
        if candidate.get('url') not in fixed:
            continue
        rid = candidate.get('room_id') or candidate['url'].rstrip('/').rsplit('/', 1)[-1]
        if rid not in rooms:
            rooms[rid] = dict(room_id=rid, title=candidate.get('title') or rid, url=candidate['url'],
                cover_url='', status='waiting', record_mode=platform['policy'].get('record_mode', 'audio'),
                asr_enabled=platform['policy'].get('asr_enabled', False), preview_enabled=False,
                recording_status='disabled', recorded_seconds=0, recording_bytes=0, recording_files=0,
                error=None, chunk_seconds=5, segments=[], items=[])
        room = rooms[rid]
        if (room['status'] == 'stopped' and platform['policy'].get('enabled')
                and candidate.get('live_status') in (0, 2) and not candidate.get('blocked')):
            room.update(status='waiting', error=None)
        if candidate.get('blocked') or candidate.get('blocked_alias'):
            room.update(status='error', error=candidate.get('last_error') or candidate.get('status_error') or '需要人工处理')
        elif candidate.get('status_error'):
            room.update(status='error', error=candidate['status_error'])
    for rid, room in rooms.items():
        room.update(instance_id=instance['id'], instance_label=label, account_mode=mode)
        if rid in instance.get('archive', {}).get('rooms', {}):
            room['home_url'] = f'rooms/{rid}/'
        records = [r for r in automation.get('session_workflows', []) if r.get('room_id') == rid]
        if records:
            latest = max(records, key=lambda r: r.get('start', ''))
            key = latest.get('key', '')
            if re.fullmatch(r'[a-f0-9]{24}', key) and instance.get('session_root'):
                root = Path(instance['session_root']).resolve()
                directory = (root / key).resolve()
                if directory.is_relative_to(root):
                    status = directory / 'workflow.json'
                    room['workflow_stage'] = latest.get('stage')
                    try:
                        if status.exists():
                            saved = json.loads(status.read_text())
                            if isinstance(saved, dict):
                                room['workflow_stage'] = saved.get('stage', latest.get('stage'))
                    except (OSError, ValueError):
                        logging.getLogger(__name__).warning('房间 %s 的场次状态不可读，保留采集状态', rid)
                    if (directory / 'public/index.html').is_file():
                        room['result_url'] = f'sessions/{key}/'
    return {**state, 'rooms': list(rooms.values()), 'instance_label': label}


def create_gateway(config, *, transport=None):
    instances = validate_instances(config['instances'])

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False, transport=transport) as client:
            app.state.client = client
            yield

    app = FastAPI(title='B站统一监控看板', lifespan=lifespan)
    archive_cache = {}
    archive_lock = asyncio.Lock()

    def instance(ident):
        if ident not in instances:
            raise HTTPException(404, '未知监控实例')
        return instances[ident]

    async def snapshot(row):
        responses = await asyncio.gather(*(app.state.client.get(row['url'] + path)
            for path in ('/api/state', '/api/platform', '/api/automation')))
        for response in responses:
            response.raise_for_status()
        return room_snapshot(*(response.json() for response in responses), row)

    def archive_room(ident, room_id):
        row = instance(ident)
        archive = row.get('archive', {})
        if (room_id not in archive.get('rooms', {}) or not re.fullmatch(r'\d+', room_id)
                or not row.get('session_root') or not archive.get('database')):
            raise HTTPException(404, '未配置房间主页')
        return row, archive

    @app.get('/instances/{ident}/api/rooms/{room_id}/archive')
    async def room_archive(ident: str, room_id: str):
        row, archive = archive_room(ident, room_id)
        async with archive_lock:
            cached = archive_cache.get((ident, room_id))
            if cached and time.monotonic() - cached[0] < 30:
                return cached[1]
            try:
                from .room_archive import archive_data
                summary_file = archive.get('summary_files', {}).get(room_id)
                data = await asyncio.to_thread(archive_data, row['session_root'], archive['database'],
                                               room_id, archive['rooms'][room_id], not bool(summary_file))
                data['mode'] = row.get('mode', 'account')
                if summary_file:
                    from .archive_summary import shared_summaries
                    data['summaries'] = await asyncio.to_thread(shared_summaries, summary_file,
                        [{**s, 'start_utc': s['start']} for s in data['sessions']],
                        archive.get('summary_origins', {}).get(room_id))
                    data['summary_source'] = 'shared'
            except (OSError, ValueError, TypeError) as error:
                raise HTTPException(503, '暂时无法读取房间归档，请稍后刷新') from error
            archive_cache[ident, room_id] = time.monotonic(), data
            return data

    @app.get('/instances/{ident}/rooms/{room_id}/')
    async def room_home(ident: str, room_id: str):
        archive_room(ident, room_id)
        return FileResponse(Path(__file__).resolve().parents[2] / 'frontend/dist/index.html')

    @app.get('/api/dashboard/instances')
    async def instance_list():
        return [{'id': row['id'], 'label': row['label'], 'mode': row.get('mode', 'account'),
                 'base': f'instances/{row["id"]}/'} for row in instances.values()]

    @app.get('/instances/{ident}/api/state')
    async def state(ident: str):
        try:
            return await snapshot(instance(ident))
        except httpx.HTTPError as error:
            raise HTTPException(502, '监控实例暂不可用') from error

    @app.get('/instances/{ident}/api/events')
    async def events(ident: str, request: Request):
        row = instance(ident)

        async def stream():
            while not await request.is_disconnected():
                try:
                    data = await snapshot(row)
                    yield 'event: snapshot\ndata: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
                except (httpx.HTTPError, OSError, ValueError):
                    yield 'event: unavailable\ndata: {}\n\n'
                await asyncio.sleep(2)
        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.get('/instances/{ident}/sessions/{key}/')
    @app.get('/instances/{ident}/sessions/{key}/{asset}')
    async def session_file(ident: str, key: str, asset: str = 'index.html'):
        row = instance(ident)
        if not re.fullmatch(r'[a-f0-9]{24}', key) or asset not in ('index.html', 'snapshot.json') or not row.get('session_root'):
            raise HTTPException(404, '结果不存在')
        root = Path(row['session_root']).resolve()
        path = (root / key / 'public' / asset).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, '结果尚未生成')
        if asset == 'index.html' and row.get('archive'):
            from .room_archive import read_json, read_object
            from .session_overview import session_overview
            try:
                config = read_json(root, root / key / 'config.json')
                rid = str(config.get('room_id'))
                if rid in row['archive'].get('rooms', {}):
                    label = html.escape(row['archive']['rooms'][rid])
                    link = f'/instances/{ident}/rooms/{rid}/'
                    nav = f'<nav style="max-width:1120px;margin:12px auto"><a href="{link}">← {label} · 按日归档</a></nav>'
                    try:
                        snapshot = read_object(root, root / key / 'public/snapshot.json')
                    except (OSError, ValueError, TypeError):
                        snapshot = {}
                    page = session_overview(path.read_text(), row['archive']['rooms'][rid], snapshot)
                    return HTMLResponse(page.replace('<main', nav + '<main', 1))
            except (OSError, ValueError, TypeError):
                pass
        return FileResponse(path)

    async def relay(row, path, request):
        url = row['url'] + path
        if request.url.query:
            url += '?' + request.url.query
        try:
            upstream = await app.state.client.send(app.state.client.build_request(request.method, url,
                content=await request.body(), headers={'Content-Type': request.headers.get('content-type', 'application/json')}), stream=True)
        except httpx.HTTPError as error:
            raise HTTPException(502, '监控实例暂不可用') from error

        async def body():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
        headers = {key: value for key, value in upstream.headers.items()
                   if key.lower() in ('content-type', 'content-disposition', 'content-encoding')}
        return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)

    @app.get('/instances/{ident}/assets/resource_metrics.json')
    async def resource_metrics(ident: str, request: Request):
        return await relay(instance(ident), '/assets/resource_metrics.json', request)

    @app.api_route('/instances/{ident}/api/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH'])
    async def proxy(ident: str, path: str, request: Request):
        row = instance(ident)
        if (any(part in ('.', '..') for part in path.split('/'))
                or path.split('/')[0] not in ('platform', 'automation', 'rooms', 'export', 'discover', 'assistant')):
            raise HTTPException(404, '不支持的监控接口')
        if row.get('mode') == 'anonymous' and request.method != 'GET' and path.startswith(('automation', 'assistant')):
            raise HTTPException(403, '匿名实例不提供账号、欢迎或点赞操作')
        summary_response = ((path == 'automation' and request.method == 'GET')
                            or (path == 'automation/account' and request.method == 'POST')
                            or (re.fullmatch(r'automation/rooms/[^/]+', path) and request.method == 'PUT'))
        if summary_response:
            try:
                response = await app.state.client.request(request.method, row['url'] + '/api/' + path,
                    content=await request.body(), headers={'Content-Type': 'application/json'})
                response.raise_for_status()
                return JSONResponse(browser_automation(response.json()), status_code=response.status_code)
            except httpx.HTTPStatusError as error:
                raise HTTPException(error.response.status_code, '自动化操作未完成，请检查本机日志') from error
            except (httpx.HTTPError, ValueError) as error:
                raise HTTPException(502, '自动化状态暂不可用') from error
        return await relay(row, '/api/' + path, request)

    dist = Path(__file__).resolve().parents[2] / 'frontend/dist'
    if dist.is_dir():
        app.mount('/', StaticFiles(directory=dist, html=True))
    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8767)
    args = parser.parse_args()
    uvicorn.run(create_gateway(json.loads(args.config.read_text())), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
