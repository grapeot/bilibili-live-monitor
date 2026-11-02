"""可复用助手 router 和不拥有采集进程的 FastAPI sidecar。"""
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from anyio import CancelScope
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import StrictBool
from starlette.background import BackgroundTask

from .assistant import AssistantStore, Conflict, Strict


class ManualEdit(Strict):
    session: str
    expected_version: int
    status: str | None = None


class PronunciationEdit(Strict):
    session: str
    name: str
    reading: str


class ControlEdit(Strict):
    session: str
    enabled: StrictBool


def assistant_router(db_path):
    router = APIRouter(prefix='/api/assistant')
    paths = list(db_path) if isinstance(db_path, (list, tuple)) else [db_path]

    def operate(fn, session=None):
        stores = [AssistantStore(path) for path in paths]
        try:
            if session is None:
                return [s.get('config') for s in stores if s.get('config')]
            matches = [s for s in stores if (s.get('config') or {}).get('session_id') == session]
            if len(matches) != 1:
                raise ValueError('场次不匹配或配置重复')
            store = matches[0]
            return fn(store)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            for store in stores:
                store.close()

    @router.get('/sessions')
    def sessions():
        return operate(None)

    @router.get('/state')
    def state(session: str):
        def read(s):
            if session != (s.get('config') or {}).get('session_id'):
                raise ValueError('场次不匹配')
            return s.state()
        return operate(read, session)

    @router.patch('/cards/{cid}')
    def edit(cid: str, data: ManualEdit):
        return operate(lambda s: s.manual(data.session, cid, data.expected_version, data.status), data.session)

    @router.get('/control')
    def control(session: str):
        return operate(lambda s: s.control(), session)

    @router.put('/control')
    def set_control(data: ControlEdit):
        return operate(lambda s: s.set_enabled(data.enabled, reason='explicit_api'), data.session)

    @router.post('/cards/{cid}/undo')
    def undo(cid: str, data: ManualEdit):
        return operate(lambda s: s.manual(data.session, cid, data.expected_version, undo=True), data.session)

    @router.put('/pronunciation')
    def pronunciation(data: PronunciationEdit):
        def update(s):
            if data.session != (s.get('config') or {}).get('session_id'):
                raise ValueError('场次不匹配')
            if not 0 < len(data.name) <= 100 or len(data.reading) > 300:
                raise ValueError('昵称或读音长度不合法')
            with s.db:
                if data.reading:
                    s.db.execute('INSERT OR REPLACE INTO overrides VALUES(?,?)', (data.name, data.reading))
                else:
                    s.db.execute('DELETE FROM overrides WHERE name=?', (data.name,))
            return {'ok': True}
        return operate(update, data.session)

    return router


HOP_HEADERS = {b'connection', b'keep-alive', b'proxy-authenticate', b'proxy-authorization',
               b'te', b'trailer', b'transfer-encoding', b'upgrade', b'host'}


def end_to_end(headers):
    connection = set()
    for k, v in headers:
        if k.lower() == b'connection':
            connection.update(x.strip().lower() for x in v.split(b','))
    return [(k, v) for k, v in headers if k.lower() not in HOP_HEADERS | connection]


def create_sidecar(db_path, upstream='http://127.0.0.1:8765', static_dir='frontend/dist', client=None):
    @asynccontextmanager
    async def lifespan(app):
        paths = list(db_path) if isinstance(db_path, (list, tuple)) else [db_path]
        for path in paths:
            store = AssistantStore(path)
            try:
                store.set_enabled(False, reason='sidecar_started')
            finally:
                store.close()
        if client is None:
            app.state.proxy = httpx.AsyncClient(timeout=httpx.Timeout(10, read=None))
        else:
            app.state.proxy = client
        try:
            yield
        finally:
            if client is None:
                await app.state.proxy.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(assistant_router(db_path))

    @app.api_route('/api/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
    async def proxy(path: str, request: Request):
        if path == 'assistant' or path.startswith('assistant/'):
            raise HTTPException(404, '未知助手 API')
        return await forward('/api/'+path, request)

    @app.get('/assets/resource_metrics.json')
    async def metrics(request: Request):
        return await forward('/assets/resource_metrics.json', request)

    async def forward(path: str, request: Request):
        transport = client or request.app.state.proxy
        url = httpx.URL(upstream.rstrip('/')+path).copy_with(query=request.url.query.encode())
        req = transport.build_request(request.method, url,
                                      headers=end_to_end(request.headers.raw), content=request.stream())
        try:
            response = await transport.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise HTTPException(502, '上游连接失败') from exc

        async def body():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                with CancelScope(shield=True):
                    await response.aclose()

        result = StreamingResponse(body(), status_code=response.status_code,
                                   background=BackgroundTask(response.aclose))
        result.raw_headers = end_to_end(response.headers.raw)
        return result

    if Path(static_dir).is_dir():
        app.mount('/', StaticFiles(directory=static_dir, html=True), name='frontend')
    return app
