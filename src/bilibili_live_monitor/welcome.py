"""显式启动的一次性欢迎助手，复用已登录Chrome，不启动常驻发送循环。"""
import asyncio
import fcntl
import json
import re
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright

from .danmaku import chat_message, entry_message, gift_message, like_message
from .gift_protocol import gift_items

INTRO = '这里是古风唱歌直播间，点亮粉丝灯牌就可以免费点歌，想听什么可以告诉主播～'


def text_length(text):
    """网页弹幕宽度：ASCII计1，非ASCII的每个UTF-16单元计2。"""
    return sum(1 if ord(char) < 128 else 2 if ord(char) <= 0xffff else 4 for char in text)


def welcome_messages(names, limit):
    """优先一条；平台字数不足才拆两条，昵称仅用于展示。"""
    if limit < 1 or text_length(INTRO) > limit:
        raise ValueError('介绍文案超出页面字数限制，需要人工调整')
    selected = [re.sub(r'[\r\n\t]', ' ', name).strip() for name in names[:3]]
    while selected:
        joined = '、'.join(selected)
        suffix = '等新来的小伙伴～' if len(names) > len(selected) else '来到直播间～'
        greeting = f'欢迎 {joined} {suffix}'
        if text_length(greeting) <= limit:
            break
        selected.pop()
    if not selected:
        raise ValueError('昵称超出页面字数限制')
    combined = greeting+INTRO
    return [combined] if text_length(combined) <= limit else [greeting, INTRO]


class WelcomeDecision:
    def __init__(self, seen, *, minimum=4, window=30, cooldown=300, last_sent=None):
        if minimum < 1 or window <= 0 or cooldown < 0:
            raise ValueError('欢迎触发参数越界')
        self.seen = set(seen)
        self.minimum, self.window, self.cooldown = minimum, window, cooldown
        self.last_sent = last_sent
        self.pending = deque()
        self.fired = False

    def observe(self, uid, name, received, now):
        if self.fired or not uid or uid in self.seen:
            return None
        self.seen.add(uid)
        self.pending.append((received, uid, name))
        while self.pending and self.pending[0][0] < now-self.window:
            self.pending.popleft()
        if len(self.pending) < self.minimum or (self.last_sent is not None and now-self.last_sent < self.cooldown):
            return None
        self.fired = True
        return list(self.pending)


def correct_page(url, room_id):
    parsed = urlsplit(url)
    return parsed.scheme == 'https' and parsed.hostname == 'live.bilibili.com' and parsed.path.rstrip('/') == '/'+room_id


def authorized_send(request, room_id, text):
    url = urlsplit(request.url)
    fields = parse_qs(request.post_data or '', keep_blank_values=True)
    return (request.method == 'POST' and url.scheme == 'https' and url.hostname == 'api.live.bilibili.com'
            and url.path == '/msg/send' and fields.get('roomid') == [room_id] and fields.get('msg') == [text])


def send_endpoint(url):
    # 按pathname匹配，查询参数不能绕过实际请求的校验。
    return urlsplit(url).path == '/msg/send'


def observations(packet, received):
    """基线与增量共用同一口径，按原始接收顺序识别入场和其他活动。"""
    command = packet.get('cmd', '')
    if command in ('INTERACT_WORD', 'INTERACT_WORD_V2'):
        item = entry_message(packet, received_utc=received)
        return [item] if item else []
    if str(command).startswith('DANMU_MSG'):
        item = chat_message(packet['info'])
        return [item] if item else []
    if command == 'LIKE_INFO_V3_CLICK':
        return [like_message(packet)]
    if command in ('SEND_GIFT', 'GUARD_BUY', 'SUPER_CHAT_MESSAGE'):
        return [gift_message(packet)]
    if command == 'SEND_GIFT_V2':
        return [gift_message({'cmd': 'SEND_GIFT', 'data': data}) for data in gift_items(packet['data'])]
    return []


INPUT_LIMIT_SCRIPT = r'''els => els.map(e=>{
    if(e.maxLength>0) return e.maxLength;
    let node=e;
    while(node && !node.__vue__) node=node.parentElement;
    const limit=node && node.__vue__.inputLengthLimit;
    if(Number.isInteger(limit) && limit>0) return limit;
    const t=e.parentElement.parentElement.innerText; const m=t.match(/\d+\s*\/\s*(\d+)/);
    return m?Number(m[1]):0;
})'''


async def message_limit(page):
    limits = await page.locator('.chat-input:visible').evaluate_all(INPUT_LIMIT_SCRIPT)
    if len(limits) != 1 or limits[0] <= 0:
        raise RuntimeError('未能读取唯一可见输入框的字数限制')
    return limits[0]


async def send_chat(page, text, *, room_id):
    if not correct_page(page.url, room_id):
        raise RuntimeError('目标页面已经切换，停止发送')
    field = page.locator('.chat-input:visible')
    if await field.count() != 1 or await field.input_value():
        raise RuntimeError('输入框不唯一或含已有草稿，停止发送')
    if await page.locator('button.send-btn:visible').count() != 1:
        raise RuntimeError('发送按钮不唯一')
    if text_length(text) > await message_limit(page):
        raise RuntimeError('文案超出当前页面字数限制')
    accepted = None
    async def guard(route):
        nonlocal accepted
        if accepted is not None or not correct_page(page.url, room_id) or not authorized_send(route.request, room_id, text):
            await route.abort()
        else:
            accepted = route.request
            await route.continue_()
    await page.route(send_endpoint, guard)
    try:
        await field.evaluate('''(e,text) => {
            if(e.value) throw new Error('输入框已有草稿');
            const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
            Object.getOwnPropertyDescriptor(proto,'value').set.call(e,text);
            e.dispatchEvent(new Event('input',{bubbles:true}));
        }''', text)
        if not correct_page(page.url, room_id) or await field.input_value() != text:
            raise RuntimeError('页面或草稿已变化，停止发送')
        async with page.expect_response(lambda r: r.request is accepted and authorized_send(r.request, room_id, text), timeout=15000) as pending:
            await page.locator('button.send-btn:visible').click()
        response = await pending.value
        result = await response.json()
        if response.status != 200 or result.get('code') != 0 or response.request is not accepted:
            raise RuntimeError(f'发送未确认成功：HTTP {response.status}, code={result.get("code")}')
        return {'http_status': response.status, 'code': result['code'], 'room_id': room_id, 'message': text}
    finally:
        await page.unroute(send_endpoint, guard)


def save(path, data):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.replace(path)


async def run_once(database: Path, room_id: str, cdp_url: str, state_path: Path, *, execute=False,
                   since, minimum=4, window=30, cooldown=300, wait_seconds=600):
    if not room_id.isdigit():
        raise ValueError('房间号必须为数字')
    if wait_seconds <= 0:
        raise ValueError('等待时间必须大于零')
    start = datetime.fromisoformat(since)
    if start.tzinfo is None:
        raise ValueError('观察起点必须包含时区')
    if start > datetime.now(timezone.utc):
        raise ValueError('观察起点不能在未来')
    since = start.astimezone(timezone.utc).isoformat()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(state_path.suffix+'.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = json.loads(state_path.read_text()) if state_path.exists() else {}
        if previous.get('status') in ('sending', 'sent', 'ambiguous'):
            raise RuntimeError('这份一次性任务已尝试发送，禁止重复；先人工核验回执')
        db = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)
        try:
            db.execute('BEGIN')
            seen = {r[0] for r in db.execute("SELECT DISTINCT json_extract(data,'$.sender_uid') FROM timeline WHERE room_id=? AND julianday(json_extract(data,'$.received_utc'))>=julianday(?)", (room_id,since)) if r[0]}
            cursor = db.execute('SELECT COALESCE(MAX(id),0) FROM raw_events WHERE room_id=?', (room_id,)).fetchone()[0]
            invalid_packets = 0
            for received, raw in db.execute("SELECT received_utc,data FROM raw_events WHERE room_id=? AND id<=? AND julianday(received_utc)>=julianday(?) ORDER BY id", (room_id,cursor,since)):
                try:
                    for item in observations(json.loads(raw), received):
                        if item['sender_uid']:
                            seen.add(item['sender_uid'])
                except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, OSError):
                    invalid_packets += 1
            db.commit()
            decision = WelcomeDecision(seen, minimum=minimum, window=window, cooldown=cooldown,
                                       last_sent=previous.get('sent_at'))
            status = {'status': 'armed' if execute else 'dry_run', 'room_id': room_id,
                      'execute': execute, 'minimum': minimum, 'window': window, 'cooldown': cooldown,
                      'armed_at': time.time(), 'baseline_raw_id': cursor, 'baseline_users': len(seen),
                      'invalid_packets': invalid_packets, 'since': since}
            if not execute:
                return status
            save(state_path, status)
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(cdp_url)
                pages = [p for c in browser.contexts for p in c.pages if
                         correct_page(p.url, room_id)]
                if len(pages) != 1:
                    raise RuntimeError('目标直播页不唯一或不存在')
                page = pages[0]
                limit = await message_limit(page)
                deadline = time.monotonic()+wait_seconds
                while time.monotonic() < deadline:
                    for rid, received, raw in db.execute("SELECT id,received_utc,data FROM raw_events WHERE room_id=? AND id>? AND julianday(received_utc)>=julianday(?) ORDER BY id", (room_id,cursor,since)).fetchall():
                        cursor = rid
                        try:
                            items = observations(json.loads(raw), received)
                            item = next((i for i in items if i.get('kind') == 'entry'), None)
                            if not item:
                                decision.seen.update(i['sender_uid'] for i in items if i['sender_uid'])
                                continue
                        except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, OSError):
                            status['invalid_packets'] += 1
                            continue
                        if not item:
                            continue
                        entrants = decision.observe(item['sender_uid'], item['author'],
                                                    datetime.fromisoformat(received).timestamp(), time.time())
                        if not entrants:
                            continue
                        texts = welcome_messages([e[2] for e in entrants], limit)
                        status.update(status='sending', target_uids=[e[1] for e in entrants],
                                      trigger_raw_id=rid, messages=texts, acknowledgments=[], limit=limit)
                        save(state_path, status)  # 先留发送意图，异常退出后不可自动重发。
                        try:
                            for text in texts:
                                status['acknowledgments'].append(await send_chat(page, text, room_id=room_id))
                                save(state_path, status)
                                if len(texts) > 1:
                                    await asyncio.sleep(2)
                        except BaseException as error:
                            status.update(status='ambiguous', error=f'{type(error).__name__}: {error}')
                            save(state_path, status)
                            raise
                        status.update(status='sent', sent_at=time.time())
                        save(state_path, status)
                        return status
                    await asyncio.sleep(1)
                status.update(status='expired')
                save(state_path, status)
                return status
        finally:
            db.close()
