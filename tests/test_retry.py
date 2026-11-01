import asyncio
from types import SimpleNamespace

from bilibili_live_monitor.browser import Browser
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.monitor import Monitor
from bilibili_live_monitor.platform import Candidate, Platform, Policy
from bilibili_live_monitor.retry import backoff_delay
from bilibili_live_monitor.store import Store


def test_backoff_caps_at_five_minutes():
    assert [backoff_delay(n) for n in range(1, 13)] == [2,4,8,16,32,64,128,256,300,300,300,300]
    assert Settings().max_reconnects == 20


async def test_twentieth_failure_stops_without_twenty_first_attempt(tmp_path, monkeypatch):
    store = Store(tmp_path/'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    monitor.generations['123456'] = 'generation'
    room = {'room_id':'123456','status':'capturing','record_mode':'none','asr_enabled':False}
    waits, calls = [], []
    async def sleep(seconds):
        waits.append(seconds)
    async def cycles(*args):
        calls.append(1)
        raise RuntimeError('synthetic stall')
    class Page:
        closed = False
        async def reload(self, **kwargs):
            pass
        def is_closed(self):
            return self.closed
        async def close(self):
            self.closed = True
    monkeypatch.setattr('bilibili_live_monitor.monitor.asyncio.sleep', sleep)
    monitor.session_cycles = cycles
    await monitor.capture(room, Page(), 'generation')
    assert len(calls) == 20 and waits == [backoff_delay(n) for n in range(1,20)]
    assert room['reconnect_attempts'] == 20 and room['next_retry_utc'] is None
    assert room['status'] == 'error' and room['error'].startswith('retry_exhausted:')
    platform = Platform(monitor)
    platform.import_candidates([Candidate(url='123456')])
    platform.configure(Policy(mode='fixed', enabled=True, target_rooms=1))
    c = next(iter(platform.candidates.values()))
    platform.failed(c, room['error'])
    assert c['blocked']
    assert Platform(monitor).candidates[c['url']]['blocked']
    platform.resume_candidate('123456')
    assert store.rooms()[0]['reconnect_attempts'] == 0
    store.close()


async def test_closed_capture_page_is_recreated_with_message_callback(tmp_path):
    store = Store(tmp_path/'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path, retry_delay=0), store)
    monitor.generations['123456'] = 'generation'
    room = {'room_id':'123456','url':'https://live.bilibili.com/123456',
            'status':'capturing','record_mode':'none','asr_enabled':False}
    class Page:
        closed = False
        url = room['url']
        def is_closed(self):
            return self.closed
        async def close(self):
            self.closed = True
    old, new = Page(), Page()
    calls = []
    async def cycles(room, page, generation):
        calls.append(page)
        if page is old:
            page.closed = True
            raise RuntimeError('page lost')
        raise asyncio.CancelledError()
    async def recreate(url, **kwargs):
        kwargs['on_danmaku']({'text':'恢复后的弹幕','source_key':'restored',
                             'at_utc':'2026-11-01T00:00:00+00:00'})
        return new
    monitor.session_cycles = cycles
    monitor.browser.page = recreate
    try:
        await monitor.capture(room, old, 'generation')
    except asyncio.CancelledError:
        pass
    assert calls == [old,new] and new.closed
    assert store.items('123456')[0]['text'] == '恢复后的弹幕'
    store.close()


async def test_browser_keeps_anchor_after_all_live_pages_close():
    class Page:
        closed = False
        url = 'about:blank'
        async def goto(self, url):
            self.url = url
        def is_closed(self):
            return self.closed
    class Context:
        pages = []
        async def new_page(self):
            page = Page()
            self.pages.append(page)
            return page
    context = Context()
    browser = Browser('unused')
    browser.browser = SimpleNamespace(is_connected=lambda:True, contexts=[context])
    assert await browser.connect() is context
    live = await context.new_page()
    live.closed = True
    await browser.connect()
    assert len([p for p in context.pages if not p.closed]) == 1
    assert browser.anchor.url == 'about:blank#live-monitor-anchor'
