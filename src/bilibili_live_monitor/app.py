import asyncio
import fcntl
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator

from .accounts import Accounts
from .automation import Automation
from .browser import room_url
from .config import load_settings
from .models import CandidateResponse, HealthResponse, RoomResponse, StateResponse
from .monitor import Monitor
from .platform import Candidate, Decision, Platform, Policy
from .room_config import validate_rooms
from .store import Store


class RobotControl(BaseModel):
    enabled: StrictBool


class LikeRequest(BaseModel):
    count: StrictInt = Field(ge=1, le=1000)
    request_id: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')


class AccountRequest(BaseModel):
    profile: str


class AddRoom(BaseModel):
    record_mode: Literal['none', 'audio', 'video'] | None = None
    asr_enabled: bool | None = None
    preview_enabled: bool | None = None
    url: str = Field(min_length=1, max_length=300, description='直播间 HTTPS 链接或正整数房间号',
                     examples=['https://live.bilibili.com/123456'])
    chunk_seconds: int | None = Field(default=None, ge=5, le=60,
                                     description='旧参数兼容接收；实时流式输入固定5秒', examples=[5])

    @field_validator('url')
    @classmethod
    def validate_url(cls, value):
        return room_url(value)


class ErrorResponse(BaseModel):
    detail: str = Field(description='失败原因')


class Candidates(BaseModel):
    candidates: list[Candidate] = Field(max_length=500)


def sse(kind, data, event_id=None):
    prefix = f'id: {event_id}\n' if event_id is not None else ''
    return f'{prefix}event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'


def create_app(settings=None):
    settings = settings or load_settings()
    config = json.loads(Path(settings.automation_config).read_text()) if settings.automation_config else {}
    validate_rooms(config.get('rooms', {}))
    paths = [Path(p['path']).expanduser().resolve() for p in config.get('profiles', {}).values()]
    if len(paths) != len(set(paths)):
        raise ValueError('不同账号必须使用不同profile目录')

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        lock = (settings.data_dir / '.service.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        accounts = Accounts(settings, config)
        try:
            from .assistant import AssistantStore
            for path in config.get('assistant_databases', []):
                assistant_store = AssistantStore(Path(path))
                try:
                    assistant_store.set_enabled(False, reason='persistent_service_started')
                finally:
                    assistant_store.close()
            await accounts.start()
            store = Store(settings.data_dir / 'monitor.sqlite3')
            monitor = Monitor(settings, store)
            app.state.monitor = monitor
            await monitor.start()
            platform = Platform(monitor)
            app.state.platform = platform
            await platform.start()
            automation = Automation(platform, accounts, config)
            app.state.automation = automation
            await automation.start()
            try:
                yield
            finally:
                await automation.close()
                await platform.close()
                await monitor.close()
                store.close()
        finally:
            await accounts.close()
            lock.close()

    app = FastAPI(title='B站直播静音转写', root_path=settings.root_path, lifespan=lifespan)
    from .assistant_api import assistant_router
    app.include_router(assistant_router([Path(p) for p in config.get('assistant_databases', [])]))

    @app.get('/api/automation', summary='自动欢迎、账号和点赞任务状态')
    async def automation_state():
        return app.state.automation.summary()

    @app.post('/api/automation/reload', summary='从启动指定的本地文件热加载房间配置')
    async def automation_reload():
        if not settings.automation_config:
            raise HTTPException(409, '未指定AUTOMATION_CONFIG')
        try:
            config = json.loads(Path(settings.automation_config).read_text())
            return await app.state.automation.reload_rooms(config)
        except (ValueError, OSError) as error:
            raise HTTPException(422, str(error)) from error

    @app.put('/api/automation/rooms/{room_id}', summary='动态开关本房间自动欢迎')
    async def automation_control(room_id: str, body: RobotControl):
        try:
            return await app.state.automation.set_enabled(room_id, body.enabled)
        except KeyError as error:
            raise HTTPException(404, '未配置此房间') from error

    @app.post('/api/automation/rooms/{room_id}/likes', status_code=202, summary='明确次数的一次性点赞任务')
    async def likes(room_id: str, body: LikeRequest):
        try:
            return app.state.automation.start_likes(room_id, body.count, body.request_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post('/api/automation/likes/{request_id}/stop', summary='停止尚未完成的点赞')
    async def stop_likes(request_id: str):
        try:
            return await app.state.automation.cancel_likes(request_id)
        except KeyError as error:
            raise HTTPException(404, '任务不存在') from error

    @app.post('/api/automation/account', summary='切换预先注册的独立账号profile')
    async def switch_account(body: AccountRequest):
        try:
            return await app.state.automation.switch_account(body.profile)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post('/api/automation/account/login', summary='打开当前账号的官方登录页面')
    async def account_login():
        automation = app.state.automation
        await automation.accounts.login(automation.browser)
        return {'opened': True}

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

    @app.get('/api/platform', summary='AI巡检摘要：目标、执行、候选与最近决策')
    async def platform_summary():
        return app.state.platform.summary()

    @app.put('/api/platform/policy', summary='持久化平台目标；异步收敛而非同步就绪承诺')
    async def platform_policy(body: Policy):
        try:
            return app.state.platform.configure(body)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post('/api/platform/candidates', status_code=202, summary='导入已发现的候选，不解除封禁')
    async def platform_candidates(body: Candidates):
        try:
            return app.state.platform.import_candidates(body.candidates)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post('/api/platform/discover', status_code=202, summary='请求异步网页发现；受冷却与拒绝边界约束')
    async def platform_discover():
        app.state.platform.discovery_requested = True
        return {'accepted': True, 'completed': False}

    @app.post('/api/platform/candidates/{room_id}/stop', status_code=202,
              summary='撤销候选，异步停止执行并防止自动补回')
    async def platform_stop(room_id: str):
        try:
            return app.state.platform.stop_candidate(room_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(404, '平台候选不存在') from error

    @app.post('/api/platform/decisions', status_code=202, summary='提交带版本的AI排序、停止与发现决策')
    async def platform_decision(body: Decision):
        try:
            return app.state.platform.apply_decision(body)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post('/api/platform/candidates/{room_id}/resume', status_code=202,
              summary='操作者显式解除候选停用及状态接口拒绝')
    async def resume_candidate(room_id: str):
        try:
            return app.state.platform.resume_candidate(room_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(404, '平台候选不存在') from error

    @app.get('/api/rooms/{room_id}/guards', summary='最新舰队榜单快照；非购买流水，complete表示本次覆盖')
    async def guards(room_id: str):
        try:
            snapshot = app.state.monitor.store.latest_guards(app.state.platform.resolve_room_id(room_id))
            return {k: v for k, v in snapshot.items() if k != 'raw_pages'} if snapshot else None
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get('/api/platform/events', summary='按游标读取平台动作回执，不夹带弹幕与转写')
    async def platform_events(after: int = 0, limit: int = 100):
        if after < 0 or not 1 <= limit <= 200:
            raise HTTPException(422, 'after非负；limit为1～200')
        rows = app.state.monitor.store.db.execute(
            "SELECT id,data FROM events WHERE kind='platform_action' AND id>? ORDER BY id LIMIT ?",
            (after, limit)).fetchall()
        return [{'id': row['id'], **json.loads(row['data'])} for row in rows]

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
            app.state.platform.stop_room_intent(room_id)
            return await app.state.monitor.stop(room_id)
        except (KeyError, ValueError) as error:
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
