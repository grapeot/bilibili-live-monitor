"""显式启动的一次性欢迎助手，复用已登录Chrome，不启动常驻发送循环。"""
import asyncio
import fcntl
import json
import re
import sqlite3
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from .danmaku import entry_message

INTRO = '这里是古风唱歌直播间，点亮粉丝灯牌就可以免费点歌，想听什么可以告诉主播～'


def text_length(text):
    return len(text.encode('utf-16-le'))//2


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


async def send_chat(page, text):
    field = page.locator('.chat-input:visible')
    if await field.count() != 1 or await field.input_value():
        raise RuntimeError('输入框不唯一或含已有草稿，停止发送')
    if await page.locator('button.send-btn:visible').count() != 1:
        raise RuntimeError('发送按钮不唯一')
    await field.fill(text)
    async with page.expect_response(lambda r: r.request.method == 'POST' and
                                    urlsplit(r.url).hostname == 'api.live.bilibili.com' and
                                    urlsplit(r.url).path == '/msg/send', timeout=15000) as pending:
        await page.locator('button.send-btn:visible').click()
    response = await pending.value
    result = await response.json()
    if response.status != 200 or result.get('code') != 0:
        raise RuntimeError(f'发送未确认成功：HTTP {response.status}, code={result.get("code")}')
    return {'http_status': response.status, 'code': result['code']}


def save(path, data):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.replace(path)


async def run_once(database: Path, room_id: str, cdp_url: str, state_path: Path, *, execute=False,
                   minimum=4, window=30, cooldown=300, wait_seconds=600):
    if not room_id.isdigit():
        raise ValueError('房间号必须为数字')
    if wait_seconds <= 0:
        raise ValueError('等待时间必须大于零')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(state_path.suffix+'.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = json.loads(state_path.read_text()) if state_path.exists() else {}
        if previous.get('status') in ('sending', 'sent', 'ambiguous'):
            raise RuntimeError('这份一次性任务已尝试发送，禁止重复；先人工核验回执')
        db = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)
        try:
            seen = {r[0] for r in db.execute("SELECT DISTINCT json_extract(data,'$.sender_uid') FROM timeline WHERE room_id=?", (room_id,)) if r[0]}
            cursor = 0
            invalid_packets = 0
            for rid, received, raw in db.execute("SELECT id,received_utc,data FROM raw_events WHERE room_id=? ORDER BY id", (room_id,)):
                cursor = rid
                try:
                    packet = json.loads(raw)
                    if packet.get('cmd') in ('INTERACT_WORD', 'INTERACT_WORD_V2'):
                        item = entry_message(packet, received_utc=received)
                        if item and item['sender_uid']:
                            seen.add(item['sender_uid'])
                except (ValueError, TypeError, KeyError, AttributeError):
                    invalid_packets += 1
            decision = WelcomeDecision(seen, minimum=minimum, window=window, cooldown=cooldown,
                                       last_sent=previous.get('sent_at'))
            status = {'status': 'armed' if execute else 'dry_run', 'room_id': room_id,
                      'execute': execute, 'minimum': minimum, 'window': window, 'cooldown': cooldown,
                      'armed_at': time.time(), 'baseline_raw_id': cursor, 'baseline_users': len(seen),
                      'invalid_packets': invalid_packets}
            if not execute:
                return status
            save(state_path, status)
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(cdp_url)
                pages = [p for c in browser.contexts for p in c.pages if
                         urlsplit(p.url).hostname == 'live.bilibili.com' and urlsplit(p.url).path.rstrip('/') == '/'+room_id]
                if len(pages) != 1:
                    raise RuntimeError('目标直播页不唯一或不存在')
                page = pages[0]
                limits = await page.locator('.chat-input').evaluate_all(r'''els => els.map(e=>{
                    const t=e.parentElement.parentElement.innerText; const m=t.match(/\d+\s*\/\s*(\d+)/);
                    return e.maxLength>0?e.maxLength:m?Number(m[1]):0;
                }).filter(n=>n>0)''')
                if not limits:
                    raise RuntimeError('未能读取页面弹幕字数限制')
                limit = min(limits)
                deadline = time.monotonic()+wait_seconds
                while time.monotonic() < deadline:
                    for rid, received, raw in db.execute("SELECT id,received_utc,data FROM raw_events WHERE room_id=? AND id>? ORDER BY id", (room_id,cursor)).fetchall():
                        cursor = rid
                        try:
                            packet = json.loads(raw)
                            if packet.get('cmd') not in ('INTERACT_WORD', 'INTERACT_WORD_V2'):
                                continue
                            item = entry_message(packet, received_utc=received)
                        except (ValueError, TypeError, KeyError, AttributeError):
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
                                status['acknowledgments'].append(await send_chat(page, text))
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
