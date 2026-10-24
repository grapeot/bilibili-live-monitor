import asyncio
from types import SimpleNamespace

import pytest

from bilibili_live_monitor.browser import Browser
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.monitor import Monitor
from bilibili_live_monitor.store import Store


@pytest.mark.parametrize('navigation_status', [200, 403, 412, 429])
async def test_raw_page_accepts_media_response_without_html_video(navigation_status):
    handlers = {}

    class Page:
        closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

        def on(self, event, callback):
            handlers[event] = callback

        async def add_init_script(self, _):
            pass

        async def goto(self, *args, **kwargs):
            if navigation_status == 200:
                handlers['response'](SimpleNamespace(status=200, url='https://cdn.bilivideo.com/live.m3u8'))
            return SimpleNamespace(status=navigation_status)

        async def wait_for_function(self, *args, **kwargs):
            pytest.fail('原始录制不得等待HTML video')

        async def evaluate(self, *args):
            pytest.fail('原始录制不得强制调用播放器play')

    page = Page()
    browser = Browser('http://localhost:9222')

    class Context:
        async def new_page(self):
            return page

    async def connect():
        return Context()

    browser.connect = connect
    if navigation_status != 200:
        with pytest.raises(RuntimeError, match='recording_refused'):
            await browser.page('https://live.bilibili.com/123456', require_playback=False)
        assert page.closed
        return
    assert await browser.page('https://live.bilibili.com/123456', require_playback=False) is page
    assert page.recording_url == 'https://cdn.bilivideo.com/live.m3u8'


@pytest.mark.parametrize('asr,preview,expected', [(False, False, False), (True, False, True), (False, True, True)])
async def test_monitor_requests_playback_only_for_decoded_consumers(tmp_path, asr, preview, expected):
    store = Store(tmp_path/'monitor.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)

    async def page(*args, **kwargs):
        assert kwargs['require_playback'] is expected
        raise ValueError('synthetic_stop_before_navigation')

    monitor.browser.page = page
    try:
        with pytest.raises(ValueError, match='synthetic_stop'):
            await monitor.add('123456', 20, asr_enabled=asr, preview_enabled=preview)
    finally:
        await monitor.close()
        store.close()


async def test_recording_reconnect_does_not_need_html_video(tmp_path):
    store = Store(tmp_path/'monitor.db')
    monitor = Monitor(Settings(data_dir=tmp_path, retry_delay=0, max_reconnects=2), store)
    room = {'room_id':'123456','record_mode':'audio','asr_enabled':False,'preview_enabled':False}
    generation = 'synthetic'
    monitor.generations['123456'] = generation
    ready = asyncio.Event()
    sessions = 0

    class Page:
        recording_url = 'https://cdn.bilivideo.com/old.m3u8'
        closed = False

        async def reload(self, **kwargs):
            assert self.recording_url is None
            self.recording_url = 'https://cdn.bilivideo.com/new.m3u8'

        async def wait_for_function(self, *args, **kwargs):
            pytest.fail('原始录制重连不得等待HTML video')

        async def evaluate(self, *args):
            pytest.fail('原始录制重连不得强制play')

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    page = Page()

    async def session(*args):
        nonlocal sessions
        sessions += 1
        if sessions == 1:
            raise RuntimeError('synthetic_interruption')
        assert page.recording_url.endswith('/new.m3u8')
        ready.set()
        await asyncio.Event().wait()

    monitor.session_cycles = session
    monitor.pages['123456'] = page
    monitor.tasks['123456'] = asyncio.create_task(monitor.capture(room, page, generation))
    try:
        await asyncio.wait_for(ready.wait(), 2)
    finally:
        await monitor.close()
        store.close()
    assert sessions == 2 and page.closed


async def test_navigation_refusal_is_terminal_on_reconnect(tmp_path):
    store = Store(tmp_path/'monitor.db')
    monitor = Monitor(Settings(data_dir=tmp_path, retry_delay=0, max_reconnects=5), store)
    room = {'room_id':'123456','record_mode':'audio','asr_enabled':False,'preview_enabled':False}
    generation = 'synthetic'
    monitor.generations['123456'] = generation

    class Page:
        closed = False
        reloads = 0

        async def reload(self, **kwargs):
            self.reloads += 1
            return SimpleNamespace(status=403)

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    async def session(*args):
        raise RuntimeError('synthetic_interruption')

    page = Page()
    monitor.session_cycles = session
    try:
        await monitor.capture(room, page, generation)
        assert page.reloads == 1 and page.closed
        assert store.rooms()[0]['status'] == 'error'
        assert 'recording_refused' in store.rooms()[0]['error']
    finally:
        await monitor.close()
        store.close()
