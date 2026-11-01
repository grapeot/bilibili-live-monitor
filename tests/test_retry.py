import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

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


async def test_startup_failures_and_capture_failures_share_twenty_budget(tmp_path, monkeypatch):
    store = Store(tmp_path/'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path, retry_delay=0, record_mode='none'), store)
    platform = Platform(monitor)
    platform.import_candidates([Candidate(url='123456')])
    candidate = next(iter(platform.candidates.values()))
    starts, captures = [], []
    class Page:
        url = 'https://live.bilibili.com/123456'
        closed = False
        async def evaluate(self, *args):
            return {'title':'synthetic','cover_url':''}
        def is_closed(self):
            return self.closed
        async def close(self):
            self.closed = True
    async def page(url, **kwargs):
        starts.append(1)
        if len(starts) < 20:
            raise RuntimeError('startup timeout')
        result = Page()
        await kwargs['on_resolved'](result)
        return result
    async def cycles(*args):
        captures.append(1)
        raise RuntimeError('capture stalled')
    monitor.browser.page = page
    monitor.session_cycles = cycles
    for _ in range(20):
        await platform.launch(candidate)
    await monitor.tasks['123456']
    assert len(captures) == 1
    assert store.rooms()[0]['reconnect_attempts'] == 20
    platform.failed(candidate,store.rooms()[0]['error'])
    assert candidate['blocked']
    await monitor.close()
    store.close()


async def test_offline_cannot_clear_terminal_failure(tmp_path):
    from test_platform import fixture
    for index, reason in enumerate(['retry_exhausted: 20', 'recording_refused: HTTP 403',
                                    'recording_refused: HTTP 412', 'recording_refused: HTTP 429']):
        path = tmp_path/str(index)
        platform = fixture(path)
        platform.import_candidates([Candidate(url='123456')])
        platform.configure(Policy(mode='fixed',enabled=True,target_rooms=1))
        platform.next_status_check = float('inf')
        async def guards():
            pass
        platform.refresh_guards = guards
        candidate = next(iter(platform.candidates.values()))
        candidate.update(room_id='123456',offline_checks=2,attempts=20,live_status=0)
        platform.active[candidate['url']] = '123456'
        platform.store.room({'room_id':'123456','status':'error','error':reason,'reconnect_attempts':20})
        await platform.tick()
        assert candidate['blocked'] and candidate['attempts'] == 20
        assert platform.store.rooms()[0]['reconnect_attempts'] == 20
        candidate['live_status'] = 1
        await platform.tick()
        assert not platform.launching
        await platform.close()
        platform.store.close()


async def test_recovery_after_long_backoff_is_not_killed_by_old_watchdog(tmp_path):
    from test_platform import fixture
    platform = fixture(tmp_path)
    platform.import_candidates([Candidate(url='123456')])
    platform.configure(Policy(mode='fixed',enabled=True,target_rooms=1,record_mode='video'))
    platform.next_status_check = float('inf')
    async def guards():
        pass
    platform.refresh_guards = guards
    candidate = next(iter(platform.candidates.values()))
    candidate.update(room_id='123456',started_epoch=time.time()-301,start_seconds=0,attempts=9)
    platform.active[candidate['url']] = '123456'
    platform.monitor.tasks['123456'] = True
    platform.store.room({'room_id':'123456','status':'capturing','record_mode':'video',
                         'asr_enabled':False,'preview_enabled':False,
                         'recorded_seconds':0,'reconnect_attempts':8})
    await platform.tick()
    assert '123456' in platform.monitor.tasks and candidate['attempts'] == 9
    await platform.close()
    platform.store.close()


async def test_persisted_deadline_waits_before_opening_browser(tmp_path, monkeypatch):
    store = Store(tmp_path/'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    deadline = datetime.fromtimestamp(time.time()+300, timezone.utc).isoformat()
    store.room({'room_id':'123456','url':'https://live.bilibili.com/123456','status':'stopped',
                'next_retry_utc':deadline,'reconnect_attempts':19})
    opened, waits = [], []
    async def page(*args, **kwargs):
        opened.append(1)
        raise AssertionError('too early')
    async def sleep(delay):
        waits.append(delay)
        raise asyncio.CancelledError()
    monitor.browser.page = page
    monkeypatch.setattr('bilibili_live_monitor.monitor.asyncio.sleep', sleep)
    try:
        await monitor.add('123456',5)
    except asyncio.CancelledError:
        pass
    assert not opened and 299 < waits[0] <= 300 and not monitor.pending
    store.close()


@pytest.mark.parametrize('redirect', ['https://live.bilibili.com/999999', 'https://passport.bilibili.com/login'])
async def test_wrong_room_reconnect_never_publishes_early_messages(tmp_path, redirect):
    store = Store(tmp_path/'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path,retry_delay=0,max_reconnects=2),store)
    monitor.generations['123456'] = 'generation'
    room = {'room_id':'123456','url':'https://live.bilibili.com/123456',
            'status':'capturing','record_mode':'none','asr_enabled':False}
    class Page:
        closed = False
        url = redirect
        def is_closed(self):
            return self.closed
        async def close(self):
            self.closed = True
    async def cycles(*args):
        raise RuntimeError('lost page')
    async def page(url, **kwargs):
        kwargs['on_danmaku']({'text':'wrong room','source_key':'wrong','at_utc':'2026-11-01T00:00:00Z'})
        kwargs['on_danmaku']({'kind':'raw_event','packet':{'cmd':'SYNTHETIC'}})
        result = Page()
        replacements.append(result)
        return result
    monitor.session_cycles = cycles
    monitor.browser.page = page
    replacements = []
    monitor.pages['123456'] = Page()
    monitor.pages['123456'].closed = True
    await monitor.capture(room, monitor.pages['123456'], 'generation')
    assert not store.items('123456')
    assert store.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 0
    assert '123456' not in monitor.pages
    assert all(page.closed for page in replacements)
    store.close()


async def test_runtime_failures_are_authoritative_when_next_start_fails(tmp_path):
    store = Store(tmp_path/'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path,retry_delay=0),store)
    platform = Platform(monitor)
    platform.import_candidates([Candidate(url='123456')])
    c = next(iter(platform.candidates.values()))
    c.update(room_id='123456',attempts=1)
    store.room({'room_id':'123456','url':c['url'],'status':'stopped','reconnect_attempts':19})
    async def fail(*args, **kwargs):
        raise RuntimeError('startup failed after runtime failures')
    monitor.browser.page = fail
    await platform.launch(c)
    assert c['attempts'] == 20 and c['blocked']
    assert store.rooms()[0]['reconnect_attempts'] == 20
    store.close()


async def test_real_stable_pcm_zero_wins_over_stale_candidate_on_restart(tmp_path,monkeypatch):
    store = Store(tmp_path/'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path,retry_delay=0,queue_size=100),store)
    platform = Platform(monitor)
    platform.import_candidates([Candidate(url='123456')])
    c = next(iter(platform.candidates.values()))
    c.update(room_id='123456',attempts=20,selected=True)
    platform.save()
    room = {'room_id':'123456','url':c['url'],'status':'capturing','record_mode':'none',
            'asr_enabled':True,'reconnect_attempts':19}
    store.room(room)
    monitor.failures['123456'] = 19
    count = 0
    async def noop(*args):
        pass
    async def read(*args):
        nonlocal count
        count += 1
        if count == 13:
            raise asyncio.CancelledError()
        return bytes(160000),{'started':1000,'ended':6000,'muted':True,'paused':False,
            'track_live':True,'context_state':'running','missing_samples':0,'dropped_samples':0,
            'media_advanced':5,'pauses':0,'play_error':None,'track_muted':False,
            'track_enabled':True,'track_muted_ms':0}
    monkeypatch.setattr('bilibili_live_monitor.monitor.start_pcm',noop)
    monkeypatch.setattr('bilibili_live_monitor.monitor.close_pcm',noop)
    monkeypatch.setattr('bilibili_live_monitor.monitor.read_pcm',read)
    with pytest.raises(asyncio.CancelledError):
        await monitor.capture_cycles(room,SimpleNamespace(is_closed=lambda:False),'generation')
    assert store.rooms()[0]['reconnect_attempts'] == 0
    restored = Platform(monitor)
    current = next(iter(restored.candidates.values()))
    assert current['attempts'] == 0
    async def fail(*args, **kwargs):
        raise RuntimeError('first new failure')
    monitor.browser.page = fail
    await restored.launch(current)
    assert current['attempts'] == 1 and not current['blocked']
    await monitor.close()
    store.close()


@pytest.mark.parametrize(('count','error'), [(19,'startup timeout'), (0,'recording_refused: HTTP 403'),
                                           (0,'HTTP 412'), (0,'HTTP 429')])
def test_crash_between_retry_room_and_candidate_commits_keeps_terminal_lock(tmp_path,monkeypatch,count,error):
    store = Store(tmp_path/'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path),store)
    platform = Platform(monitor)
    platform.import_candidates([Candidate(url='123456')])
    c = next(iter(platform.candidates.values()))
    c.update(room_id='123456',attempts=count,blocked=False)
    platform.save()
    store.room({'room_id':'123456','url':c['url'],'status':'stopped',
                'reconnect_attempts':count,'error':None})
    def crash():
        raise SystemExit('crash after room commit')
    monkeypatch.setattr(platform,'save',crash)
    with pytest.raises(SystemExit):
        platform.failed(c,error)
    restored = Platform(monitor)
    recovered = next(iter(restored.candidates.values()))
    assert recovered['blocked']
    assert recovered['attempts'] == count+1
    assert recovered['last_error']
    store.close()
