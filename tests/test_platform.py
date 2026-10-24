import asyncio
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bilibili_live_monitor.app import create_app
from bilibili_live_monitor.babysitter import decide
from bilibili_live_monitor.browser import Browser
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.monitor import Monitor
from bilibili_live_monitor.platform import Candidate, Decision, Platform, Policy
from bilibili_live_monitor.store import Store


def fixture(tmp_path, **settings):
    store = Store(tmp_path/'monitor.sqlite3')
    monitor = SimpleNamespace(settings=Settings(data_dir=tmp_path, max_rooms=4, **settings),
                              store=store, tasks={}, pending=set(), queue=asyncio.Queue(),
                              asr=SimpleNamespace(ready=False))

    async def add(url, seconds, **options):
        rid = url.rsplit('/', 1)[-1]
        room = {'room_id': rid, 'url': url, 'status': 'capturing', **options}
        store.room(room)
        monitor.tasks[rid] = True
        return room

    async def stop(rid):
        monitor.tasks.pop(rid, None)
    monitor.add, monitor.stop = add, stop
    return Platform(monitor)


async def settled(platform):
    await platform.tick()
    await asyncio.gather(*platform.launching.values())
    await platform.tick()


async def test_scale_restore_stop_and_revision(tmp_path):
    platform = fixture(tmp_path)
    platform.import_candidates([Candidate(url=str(i)) for i in range(1, 7)])
    platform.configure(Policy(enabled=True, target_rooms=3))
    await settled(platform)
    assert len(platform.active) == 3
    stale = Decision(revision=platform.revision, stop=['1'], reason='合成决策')
    platform.configure(Policy(enabled=True, target_rooms=1))
    with pytest.raises(ValueError, match='过期'):
        platform.apply_decision(stale)
    await settled(platform)
    assert len(platform.active) == 1
    retained = next(iter(platform.active))
    await platform.close()
    restored = Platform(platform.monitor)
    assert restored.policy.target_rooms == 1
    assert restored.candidates[retained]['selected']
    restored.monitor.tasks.clear()
    await settled(restored)
    assert retained in restored.active
    restored.stop_candidate(retained)
    assert restored.candidates[retained]['blocked']
    restored.import_candidates([Candidate(url=retained)])
    assert restored.candidates[retained]['blocked']
    await restored.close()
    platform.store.close()


async def test_refusal_and_crash_before_reconcile_stay_blocked(tmp_path):
    platform = fixture(tmp_path)
    platform.import_candidates([Candidate(url='123456')])
    candidate = platform.candidates['https://live.bilibili.com/123456']
    candidate.update(room_id='123456', selected=True)
    platform.save()
    platform.store.room({'room_id': '123456', 'status': 'error', 'error': 'recording_refused: HTTP 403'})
    recovered = Platform(platform.monitor)
    assert recovered.candidates[candidate['url']]['blocked']
    recovered.configure(Policy(enabled=True, target_rooms=1))
    recovered.last_discovery = float('inf')
    await settled(recovered)
    assert not recovered.active
    recovered.store.close()


async def test_downscale_cancels_pending_start_without_leak(tmp_path):
    platform = fixture(tmp_path)
    cancelled = asyncio.Event()

    async def pending(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    platform.monitor.add = pending
    platform.import_candidates([Candidate(url='123456')])
    platform.configure(Policy(enabled=True, target_rooms=1))
    await platform.tick()
    await asyncio.sleep(0)
    platform.configure(Policy(enabled=True, target_rooms=0))
    await platform.tick()
    assert cancelled.is_set()
    assert not platform.launching and not platform.active
    await platform.close()
    platform.store.close()


async def test_failed_capture_is_replaced_and_retry_is_bounded(tmp_path):
    platform = fixture(tmp_path)
    platform.import_candidates([Candidate(url='1'), Candidate(url='2')])
    platform.configure(Policy(enabled=True, target_rooms=1))
    await settled(platform)
    platform.monitor.tasks.clear()
    await settled(platform)
    assert list(platform.active.values()) == ['2']
    assert platform.candidates['https://live.bilibili.com/1']['next_attempt'] > 0
    await platform.close()
    platform.store.close()


async def test_real_ai_command_and_failure_do_not_stop_recording(tmp_path):
    code = ('import sys,json; from pathlib import Path; '
            'r=json.loads(Path(sys.argv[1]).read_text()); '
            'Path(sys.argv[2]).write_text(json.dumps({"revision":r["health"]["revision"],'
            '"reason":"合成AI决策","prioritize":["123456"]}))')
    platform = fixture(tmp_path, babysitter_command=[sys.executable, '-c', code])
    platform.import_candidates([Candidate(url='123456')])
    platform.configure(Policy(enabled=True, target_rooms=1))
    await settled(platform)
    await decide(platform)
    assert platform.last_ai['status'] == 'applied'
    assert platform.candidates['https://live.bilibili.com/123456']['priority'] == 1000
    platform.settings.babysitter_command = [sys.executable, '-c', 'raise SystemExit(3)']
    await decide(platform)
    assert platform.last_ai['status'] == 'failed'
    assert len(platform.monitor.tasks) == 1
    platform.settings.babysitter_command = [sys.executable, '-c', 'import time; time.sleep(60)']
    platform.settings.babysitter_timeout_seconds = .03
    await decide(platform)
    assert platform.last_ai['status'] == 'failed'
    assert 'TimeoutError' in platform.last_ai['error']
    assert len(platform.monitor.tasks) == 1
    await platform.close()
    platform.store.close()


def test_ai_api_capacity_and_persistence(tmp_path):
    settings = Settings(data_dir=tmp_path, max_rooms=4)
    with TestClient(create_app(settings)) as client:
        response = client.put('/api/platform/policy', json={'target_rooms': 5})
        assert response.status_code == 422
        receipt = client.put('/api/platform/policy', json={'target_rooms': 3}).json()
        assert receipt['accepted']
        assert client.post('/api/platform/candidates', json={'candidates': [{'url': '123456'}]}).status_code == 202
        assert client.post('/api/platform/decisions', json={'revision': -1, 'reason': '过期'}).status_code == 409
        assert client.post('/api/platform/decisions', json={'revision': receipt['revision'],
                           'stop': ['999999'], 'reason': '未知'}).status_code == 409
        assert client.get('/api/platform/events?limit=201').status_code == 422
        events = client.get('/api/platform/events').json()
        assert events[0]['action'] == 'policy'
        assert not any('text' in e for e in events)
    with TestClient(create_app(settings)) as client:
        state = client.get('/api/platform').json()
        assert state['policy']['target_rooms'] == 3
        assert state['candidates'][0]['url'].endswith('/123456')
        assert state['babysitter']['status'] == 'not_configured'


@pytest.mark.parametrize('code', [403, 412, 429])
@pytest.mark.parametrize('homepage', [True, False])
async def test_discovery_refusal_persists_and_stops_requests(tmp_path, code, homepage):
    platform = fixture(tmp_path)
    calls = []

    class Page:
        closed = False

        async def add_init_script(self, *args):
            pass

        async def goto(self, url, **kwargs):
            calls.append(url)
            return SimpleNamespace(status=code if homepage or len(calls)>1 else 200)

        async def wait_for_function(self, *args, **kwargs):
            pass

        async def evaluate(self, *args):
            return [{'url': 'https://live.bilibili.com/123456', 'title': '合成候选'}]

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    page = Page()
    browser = Browser('http://localhost:9222')

    async def new_page():
        return page

    async def connect():
        return SimpleNamespace(new_page=new_page)

    browser.connect = connect
    platform.monitor.browser = browser
    await platform.discover()
    assert platform.discovery_blocked and page.closed
    restored = Platform(platform.monitor)
    restored.configure(Policy(enabled=True, target_rooms=1))
    restored.discovery_requested = True
    await restored.tick()
    assert restored.discovery_task is None
    assert len(calls) == (1 if homepage else 2)
    await restored.close()
    platform.store.close()


def test_legacy_stop_revokes_all_platform_aliases(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        platform = app.state.platform
        def seed():
            platform.import_candidates([Candidate(url='1'), Candidate(url='123456')])
            for candidate in platform.candidates.values():
                candidate['room_id'] = '123456'
            platform.store.room({'room_id': '123456', 'url': 'https://live.bilibili.com/123456',
                                 'title': '合成来源', 'cover_url': '', 'chunk_seconds': 30,
                                 'status': 'stopped', 'error': None, 'muted': True, 'cycle_notice': False})
        client.portal.call(seed)
        revision = platform.revision
        assert client.post('/api/rooms/123456/stop').status_code == 200
        assert all(c['blocked'] for c in platform.candidates.values())
        assert platform.revision > revision


async def test_failed_restart_preserves_media_accounting(tmp_path):
    store = Store(tmp_path/'monitor.sqlite3')
    previous = {'room_id': '123456', 'status': 'stopped', 'recorded_seconds': 900,
                'recording_files': 3, 'recording_bytes': 1200, 'recording_session': 'synthetic'}
    store.room(previous)
    monitor = Monitor(Settings(data_dir=tmp_path), store)

    async def page(url, **kwargs):
        await kwargs['on_resolved'](SimpleNamespace(url=url))
        raise RuntimeError('synthetic_startup_failure')

    monitor.browser.page = page
    with pytest.raises(RuntimeError, match='synthetic'):
        await monitor.add('123456', 30)
    await monitor.close()
    store.close()
    reopened = Store(tmp_path/'monitor.sqlite3')
    row = reopened.rooms()[0]
    for key in ('recorded_seconds', 'recording_bytes', 'recording_files', 'recording_session'):
        assert row[key] == previous[key]
    reopened.close()
