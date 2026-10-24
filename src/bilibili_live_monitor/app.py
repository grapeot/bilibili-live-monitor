import asyncio
import fcntl
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .browser import room_url
from .config import load_settings
from .models import CandidateResponse, HealthResponse, RoomResponse, StateResponse
from .monitor import Monitor
from .store import Store


class AddRoom(BaseModel):
    record_mode: Literal['none', 'audio', 'video'] | None = None
    asr_enabled: bool | None = None
    preview_enabled: bool | None = None
    url: str = Field(min_length=1, max_length=300, description='直播间 HTTPS 链接或正整数房间号',
                     examples=['https://live.bilibili.com/123456'])
    chunk_seconds: int | None = Field(default=None, ge=5, le=60,
                                     description='固定切片秒数；null使用服务默认配置', examples=[30])

    @field_validator('url')
    @classmethod
    def validate_url(cls, value):
        return room_url(value)


class ErrorResponse(BaseModel):
    detail: str = Field(description='失败原因')


def sse(kind, data, event_id=None):
    prefix = f'id: {event_id}\n' if event_id is not None else ''
    return f'{prefix}event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'


def create_app(settings=None):
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        lock = (settings.data_dir / '.service.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            store = Store(settings.data_dir / 'monitor.sqlite3')
            monitor = Monitor(settings, store)
            app.state.monitor = monitor
            await monitor.start()
            try:
                yield
            finally:
                await monitor.close()
                store.close()
        finally:
            lock.close()

    app = FastAPI(title='B站直播静音转写', root_path=settings.root_path, lifespan=lifespan)

    @app.get('/api/health', summary='检查服务与ASR状态', response_model=HealthResponse)
    async def health():
        monitor = app.state.monitor
        return {'ok': True, 'asr_enabled': monitor.metrics()['asr_enabled'],
                'asr_ready': monitor.asr.ready, 'queue_depth': monitor.queue.qsize()}

    @app.get('/api/rooms/{room_id}/preview.jpg', summary='显式启用缩略图后按需抓取')
    async def preview(room_id: str):
        try:
            room_id = room_url(room_id).rsplit('/', 1)[1]
            image = await app.state.monitor.previews.frame(room_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(404, '缩略图未启用或房间未运行') from error
        except (RuntimeError, TimeoutError) as error:
            raise HTTPException(503, '缩略图暂不可用') from error
        return Response(image, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})

    @app.get('/api/state', summary='获取房间与最近转写快照', response_model=StateResponse)
    async def state():
        return app.state.monitor.state()

    @app.get('/api/export', summary='导出SQLite中的完整混合时间线JSONL')
    async def export(room_id: str | None = None):
        if room_id is not None:
            try:
                room_id = room_url(room_id).rsplit('/', 1)[1]
            except ValueError as error:
                raise HTTPException(422, str(error)) from error

        async def lines():
            for item in app.state.monitor.store.export_items(room_id):
                yield json.dumps(item, ensure_ascii=False) + '\n'
                await asyncio.sleep(0)

        return StreamingResponse(lines(), media_type='application/x-ndjson',
                                 headers={'Content-Disposition': 'attachment; filename="timeline.jsonl"'})

    @app.get('/api/discover', summary='从直播首页发现候选房间', response_model=list[CandidateResponse],
             responses={502: {'model': ErrorResponse}})
    async def discover(limit: int = 20):
        if not 1 <= limit <= 200:
            raise HTTPException(422, 'limit需为1～200')
        try:
            return await app.state.monitor.browser.discover(limit)
        except Exception as error:
            raise HTTPException(502, f'{type(error).__name__}: {error}') from error

    @app.post('/api/rooms', summary='添加并开始监听房间', status_code=201, response_model=RoomResponse,
              responses={409: {'model': ErrorResponse}, 502: {'model': ErrorResponse}})
    async def add_room(body: AddRoom):
        try:
            options = {key: getattr(body, key) for key in ('record_mode', 'asr_enabled', 'preview_enabled')
                       if getattr(body, key) is not None}
            return await app.state.monitor.add(body.url, body.chunk_seconds or settings.chunk_seconds, **options)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            raise HTTPException(502, f'{type(error).__name__}: {error}') from error

    @app.post('/api/rooms/{room_id}/stop', summary='停止指定房间', response_model=RoomResponse,
              responses={404: {'model': ErrorResponse}})
    async def stop_room(room_id: str):
        try:
            return await app.state.monitor.stop(room_id)
        except KeyError as error:
            raise HTTPException(404, '房间不存在') from error

    @app.get('/api/events', summary='SSE快照与持久事件，支持Last-Event-ID')
    async def events(request: Request, last_event_id: int | None = Header(default=None, ge=0)):
        monitor = app.state.monitor

        async def stream():
            # 同一事件循环内取cursor和快照，中间不await，避免快照/订阅竞争丢事件。
            cursor = monitor.store.cursor() if last_event_id is None else last_event_id
            yield sse('snapshot', monitor.state())
            while not await request.is_disconnected():
                rows = monitor.store.events(cursor)
                for row in rows:
                    cursor = row['id']
                    yield sse(row['kind'], json.loads(row['data']), cursor)
                if not rows:
                    yield sse('metrics', monitor.metrics())
                    await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    dist = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'
    if dist.exists():
        app.mount('/', StaticFiles(directory=dist, html=True), name='dashboard')
    return app
