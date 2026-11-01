import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest

from bilibili_live_monitor.welcome import (
    INPUT_LIMIT_SCRIPT,
    INTRO,
    WelcomeDecision,
    authorized_send,
    run_once,
    send_chat,
    send_endpoint,
    send_fields,
    text_length,
    welcome_messages,
)


def test_unique_new_users_window_and_one_shot():
    decision = WelcomeDecision({'old'})
    assert decision.observe('old', 'old', 1, 1) is None
    assert decision.observe(None, 'anonymous', 1, 1) is None
    for i in range(3):
        assert decision.observe(str(i), str(i), i, i) is None
    assert decision.observe('0', 'reentry', 3, 3) is None
    assert len(decision.observe('3', 'new', 4, 4)) == 4
    assert decision.observe('4', 'another', 5, 5) is None


def test_window_expiry_and_cooldown():
    decision = WelcomeDecision(set(), minimum=2, last_sent=0)
    assert decision.observe('1', 'a', 280, 280) is None
    assert decision.observe('2', 'b', 290, 290) is None
    assert decision.observe('3', 'c', 301, 301) is not None
    decision = WelcomeDecision(set(), minimum=2)
    assert decision.observe('1', 'a', 0, 0) is None
    assert decision.observe('2', 'b', 31, 31) is None
    assert decision.observe('3', 'c', 32, 32) is not None


def test_message_length_and_split():
    assert len(welcome_messages(['合成访客'], 120)) == 1
    assert len(welcome_messages(['合成访客'], 80)) == 2
    split = welcome_messages(['合成访客'], text_length(INTRO))
    assert len(split) == 2 and split[1] == INTRO
    assert all(text_length(s) <= text_length(INTRO) for s in split)
    assert text_length('😀') == 4
    assert text_length('A中é') == 5
    with pytest.raises(ValueError):
        welcome_messages(['访客'], 10)


def test_input_limit_from_visible_editor_component_or_dom():
    # 执行浏览器使用的同一JS，复现真实页面无maxlength且计数器在隐藏的全屏编辑器中的情况。
    js = f'''const readLimit=({INPUT_LIMIT_SCRIPT});
      function field(native,component,counter){{
        const parent={{innerText:counter,parentElement:null}};
        if(component!==null) parent.__vue__={{inputLengthLimit:component}};
        return {{maxLength:native,parentElement:{{parentElement:parent}}}};
      }}
      console.log(JSON.stringify(readLimit([
        field(-1,80,"发送"),field(20,80,"发送"),field(-1,null,"0/60"),field(-1,null,"发送")
      ])));'''
    result = subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [80, 20, 60, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['sending', 'sent', 'ambiguous'])
async def test_existing_attempt_never_sends_again(tmp_path, status):
    state = tmp_path/'state.json'
    state.write_text(json.dumps({'status': status}))
    with pytest.raises(RuntimeError, match='禁止重复'):
        await run_once(tmp_path/'missing.sqlite', '123456', 'not-used', state, since='2026-01-01T00:00:00Z', execute=True)


@pytest.mark.asyncio
async def test_dry_run_does_not_touch_chrome(tmp_path):
    path = tmp_path/'history.sqlite'
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE timeline(room_id TEXT,data TEXT); CREATE TABLE raw_events(id INTEGER,room_id TEXT,received_utc TEXT,data TEXT);')
    db.close()
    result = await run_once(path, '123456', 'invalid-url', tmp_path/'state.json', since='2026-01-01T00:00:00Z')
    assert result['status'] == 'dry_run'
    assert not (tmp_path/'state.json').exists()


class FakeRequest(SimpleNamespace):
    @property
    def post_data_buffer(self):
        return self.post_data.encode('utf-8')


def request(room='123456', text='合成测试'):
    return FakeRequest(url='https://api.live.bilibili.com/msg/send', method='POST',
                       headers={'content-type': 'application/x-www-form-urlencoded'},
                       post_data=urlencode({'roomid': room, 'msg': text}))


def attach_cdp(page):
    cdp = SimpleNamespace(send=AsyncMock(), detach=AsyncMock())
    page.context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=cdp))
    return cdp


def multipart_request(pairs):
    req = request()
    boundary = 'synthetic-form-boundary'
    req.headers = {'content-type': f'multipart/form-data; boundary={boundary}'}
    req.post_data = ''.join(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
                            for key, value in pairs)+f'--{boundary}--\r\n'
    return req


def test_actual_browser_multipart_utf8_and_duplicate_fields():
    req = multipart_request([('msg', '合成测试😀'), ('roomid', '123456'), ('csrf', 'synthetic')])
    assert send_fields(req)['msg'] == ['合成测试😀']
    assert authorized_send(req, '123456', '合成测试😀')
    assert not authorized_send(req, '999999', '合成测试😀')
    assert not authorized_send(req, '123456', '其他文案')
    duplicate = multipart_request([('msg', '合成测试'), ('roomid', '123456'), ('roomid', '999999')])
    assert not authorized_send(duplicate, '123456', '合成测试')
    duplicate = multipart_request([('msg', '合成测试'), ('msg', '另一条'), ('roomid', '123456')])
    assert not authorized_send(duplicate, '123456', '合成测试')


def test_malformed_multipart_is_rejected():
    req = request()
    req.headers = {'content-type': 'multipart/form-data; boundary=missing'}
    assert not authorized_send(req, '123456', '合成测试')


class ResponseContext:
    def __init__(self, page, predicate, code):
        self.page, self.predicate, self.code = page, predicate, code

    @property
    def value(self):
        async def response():
            result = SimpleNamespace(status=200, request=self.page.request,
                                     json=AsyncMock(return_value={'code': self.code}))
            assert not self.predicate(SimpleNamespace(request=request(text='无关弹幕')))
            assert self.predicate(result)
            return result
        return response()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize('wire', ['urlencoded', 'multipart'])
@pytest.mark.parametrize('query', ['', '?source=web'])
@pytest.mark.parametrize('code', [0, -1])
async def test_send_uses_one_click_without_retry(code, query, wire):
    field = SimpleNamespace(count=AsyncMock(return_value=1), input_value=AsyncMock(side_effect=['', '合成测试']),
                            evaluate=AsyncMock(), evaluate_all=AsyncMock(return_value=[80]))
    button = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock())
    page = SimpleNamespace(locator=lambda s: field if 'chat-input' in s else button,
                           url='https://live.bilibili.com/123456', request=request())
    if wire == 'multipart':
        page.request = multipart_request([('msg', '合成测试'), ('roomid', '123456')])
    page.request.url += query
    cdp = attach_cdp(page)
    route = SimpleNamespace(request=page.request, abort=AsyncMock(), continue_=AsyncMock())
    async def install(matcher, guard):
        assert matcher(page.request.url)
        await guard(route)
        # 同一任务只允许一条请求，即使网页自行重试也必须拦截。
        await guard(route)
    page.route = AsyncMock(side_effect=install)
    page.unroute = AsyncMock()
    page.expect_response = lambda predicate, **k: ResponseContext(page, predicate, code)
    if code:
        with pytest.raises(RuntimeError):
            await send_chat(page, '合成测试', room_id='123456')
    else:
        assert (await send_chat(page, '合成测试', room_id='123456'))['code'] == 0
    field.evaluate.assert_awaited_once()
    button.click.assert_awaited_once()
    route.continue_.assert_awaited_once()
    route.abort.assert_awaited_once()
    page.unroute.assert_awaited_once()
    cdp.send.assert_any_await('Network.setBypassServiceWorker', {'bypass': True})
    cdp.send.assert_any_await('Network.setBypassServiceWorker', {'bypass': False})
    cdp.detach.assert_awaited_once()


@pytest.mark.parametrize('body', [urlencode({'roomid': '999999', 'msg': '合成测试'}),
                                 urlencode({'roomid': '123456', 'msg': '不同文案'}),
                                 'roomid=123456&roomid=999999&msg=x'])
def test_other_room_or_text_rejected(body):
    req = request()
    req.post_data = body
    assert not authorized_send(req, '123456', '合成测试')


@pytest.mark.asyncio
async def test_navigation_blocks_before_touching_draft():
    page = SimpleNamespace(url='https://live.bilibili.com/999999')
    with pytest.raises(RuntimeError, match='切换'):
        await send_chat(page, '合成测试', room_id='123456')


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['', '?source=web'])
@pytest.mark.parametrize('change', ['room', 'text', 'navigation'])
async def test_request_gate_blocks_changes_after_preflight(change, query):
    field = SimpleNamespace(count=AsyncMock(return_value=1), input_value=AsyncMock(side_effect=['', '合成测试']),
                            evaluate=AsyncMock(), evaluate_all=AsyncMock(return_value=[80]))
    button = SimpleNamespace(count=AsyncMock(return_value=1))
    page = SimpleNamespace(url='https://live.bilibili.com/123456',
                           locator=lambda s: field if 'chat-input' in s else button,
                           unroute=AsyncMock())
    cdp = attach_cdp(page)
    outgoing = request(room='999999' if change == 'room' else '123456',
                       text='草稿已改' if change == 'text' else '合成测试')
    outgoing.url += query
    route = SimpleNamespace(request=outgoing, abort=AsyncMock(), continue_=AsyncMock())
    async def install(matcher, guard):
        assert matcher(outgoing.url)
        page.guard = guard
    async def click():
        if change == 'navigation':
            page.url = 'https://live.bilibili.com/999999'
        await page.guard(route)
    class BlockedResponse:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            raise TimeoutError('no authorized response')
    page.route = AsyncMock(side_effect=install)
    page.expect_response = lambda *a, **k: BlockedResponse()
    button.click = AsyncMock(side_effect=click)
    with pytest.raises(TimeoutError):
        await send_chat(page, '合成测试', room_id='123456')
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()
    page.unroute.assert_awaited_once()
    cdp.send.assert_any_await('Network.setBypassServiceWorker', {'bypass': False})
    cdp.detach.assert_awaited_once()


def test_send_endpoint_query_and_other_host_cannot_bypass_guard():
    assert send_endpoint('https://api.live.bilibili.com/msg/send?source=web')
    assert send_endpoint('https://other.invalid/msg/send?source=web')
    assert not send_endpoint('https://api.live.bilibili.com/not-send')


@pytest.mark.asyncio
async def test_existing_draft_is_preserved():
    field = SimpleNamespace(count=AsyncMock(return_value=1), input_value=AsyncMock(return_value='用户草稿'),
                            evaluate=AsyncMock())
    page = SimpleNamespace(url='https://live.bilibili.com/123456', locator=lambda _: field)
    with pytest.raises(RuntimeError, match='草稿'):
        await send_chat(page, '合成测试', room_id='123456')
    field.evaluate.assert_not_awaited()


def entry(uid):
    return {'cmd': 'INTERACT_WORD', 'data': {'uid': uid, 'uname': '合成访客', 'msg_type': 1}}


def make_database(path):
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE timeline(room_id TEXT,data TEXT); CREATE TABLE raw_events(id INTEGER PRIMARY KEY,room_id TEXT,received_utc TEXT,data TEXT);')
    return db


def add_raw(db, packet, at):
    db.execute('INSERT INTO raw_events(room_id,received_utc,data) VALUES(?,?,?)',
               ('123456', at, json.dumps(packet)))
    db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_second', [False, True])
async def test_complete_one_shot_history_scope_and_incremental_activity(tmp_path, monkeypatch, fail_second):
    import bilibili_live_monitor.welcome as welcome
    path, state = tmp_path/'history.sqlite', tmp_path/'state.json'
    db = make_database(path)
    now = datetime.now(timezone.utc)
    since = (now-timedelta(hours=1)).isoformat()
    # 上场出现过的UID，本场依然有资格成为首次观察者。
    add_raw(db, entry(10), (now-timedelta(days=1)).isoformat())
    add_raw(db, entry(20), (now-timedelta(minutes=10)).isoformat())
    page = SimpleNamespace(url='https://live.bilibili.com/123456',
                           locator=lambda _: SimpleNamespace(evaluate_all=AsyncMock(return_value=[text_length(INTRO) if fail_second else 120])))
    async def connect(_):
        # 这些报文在基线建立之后才出现，且没有对应timeline行。
        at = now.isoformat()
        add_raw(db, {'cmd': 'DANMU_MSG', 'info': [[0,0,0,0,now.timestamp()], '测试', [30,'发言者'], []]}, at)
        add_raw(db, {'cmd': 'SEND_GIFT', 'data': {'uid': 40, 'uname': '送礼者', 'giftId': 1, 'giftName': '测试礼物', 'num': 1}}, at)
        add_raw(db, {'cmd': 'LIKE_INFO_V3_CLICK', 'data': {'uid': 50}}, at)
        for uid in (20, 30, 40, 50, 0, 10, 60):
            add_raw(db, entry(uid), at)
        return SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    class PlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
        async def __aexit__(self, *_):
            pass
    monkeypatch.setattr(welcome, 'async_playwright', PlaywrightContext)
    sender = AsyncMock(side_effect=[{'code': 0}, TimeoutError('synthetic timeout')] if fail_second else None,
                       return_value={'code': 0})
    monkeypatch.setattr(welcome, 'send_chat', sender)
    # 不必真的等待两条之间的2秒；不影响窗口或超时语义。
    monkeypatch.setattr(welcome.asyncio, 'sleep', AsyncMock())
    if fail_second:
        with pytest.raises(TimeoutError):
            await run_once(path, '123456', 'fake', state, since=since, minimum=1, execute=True)
        result = json.loads(state.read_text())
        assert result['status'] == 'ambiguous' and len(result['acknowledgments']) == 1
        assert sender.await_count == 2
    else:
        result = await run_once(path, '123456', 'fake', state, since=since, minimum=1, execute=True)
        assert result['status'] == 'sent' and sender.await_count == 1
    assert result['target_uids'] == ['10']
    with pytest.raises(RuntimeError, match='禁止重复'):
        await run_once(path, '123456', 'fake', state, since=since, minimum=1, execute=True)
    db.close()


@pytest.mark.asyncio
async def test_timeout_without_new_entry(tmp_path, monkeypatch):
    import bilibili_live_monitor.welcome as welcome
    path = tmp_path/'empty.sqlite'
    make_database(path).close()
    page = SimpleNamespace(url='https://live.bilibili.com/123456',
                           locator=lambda _: SimpleNamespace(evaluate_all=AsyncMock(return_value=[80])))
    class PlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(
                return_value=SimpleNamespace(contexts=[SimpleNamespace(pages=[page])]))))
        async def __aexit__(self, *_):
            pass
    monkeypatch.setattr(welcome, 'async_playwright', PlaywrightContext)
    sender = AsyncMock()
    monkeypatch.setattr(welcome, 'send_chat', sender)
    result = await run_once(path, '123456', 'fake', tmp_path/'state.json', since='2026-01-01T00:00:00Z',
                            execute=True, wait_seconds=0.001)
    assert result['status'] == 'expired'
    sender.assert_not_awaited()
