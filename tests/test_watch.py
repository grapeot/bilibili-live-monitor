import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_gifts import frame
from test_platform import fixture, settled

from bilibili_live_monitor.app import create_app
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.danmaku import messages
from bilibili_live_monitor.platform import Candidate, Platform, Policy
from bilibili_live_monitor.room_status import guard_snapshot
from bilibili_live_monitor.rotation import RoomStatusRefused, inspect
from bilibili_live_monitor.store import Store


async def status(platform, monkeypatch, value):
    async def read(*args):
        if isinstance(value, Exception):
            raise value
        return {'123456': {'room_id': 123456, 'uid': 654321, 'live_status': value, 'title': '合成房间'}}
    monkeypatch.setattr('bilibili_live_monitor.platform.room_statuses', read)
    await platform.refresh_statuses()


async def test_fixed_offline_live_offline_restart_and_live_again(tmp_path, monkeypatch):
    platform = fixture(tmp_path)
    platform.monitor.browser = object()
    platform.import_candidates([Candidate(url='123456')])
    platform.configure(Policy(mode='fixed', enabled=True, target_rooms=1))
    platform.next_status_check = float('inf')
    # 避免后台快照联网；独立测试验证快照分页。
    async def no_guards():
        pass
    platform.refresh_guards = no_guards
    candidate = platform.candidates['https://live.bilibili.com/123456']
    for _ in range(5):
        await status(platform, monkeypatch, 0)
        await settled(platform)
    assert candidate['attempts'] == 0 and not candidate['blocked'] and not platform.active
    await status(platform, monkeypatch, 1)
    await settled(platform)
    assert list(platform.active.values()) == ['123456']
    await status(platform, monkeypatch, 0)
    await platform.tick()
    assert platform.active  # 单次停播状态不截断录制。
    await status(platform, monkeypatch, 0)
    await platform.tick()
    assert not platform.active and candidate['watch_state'] == 'waiting'
    assert not candidate['blocked']
    await platform.close()
    restored = Platform(platform.monitor)
    restored.refresh_guards = no_guards
    restored.next_status_check = float('inf')
    assert restored.policy.mode == 'fixed'
    await status(restored, monkeypatch, 1)
    await settled(restored)
    assert list(restored.active.values()) == ['123456']
    await restored.close()
    restored.store.close()


async def test_fixed_transient_failure_retries_twenty_times_and_refusal_needs_resume(tmp_path):
    platform = fixture(tmp_path)
    platform.import_candidates([Candidate(url='123456')])
    platform.configure(Policy(mode='fixed', enabled=True, target_rooms=1))
    c = next(iter(platform.candidates.values()))
    for _ in range(19):
        c['attempts'] += 1
        platform.failed(c, 'network timeout')
    assert not c['blocked'] and c['next_attempt'] > 0
    c['attempts'] += 1
    platform.failed(c, 'network timeout')
    assert c['blocked'] and c['watch_state'] == 'attention'
    platform.resume_candidate('123456')
    platform.failed(c, 'recording_refused: HTTP 403')
    assert c['blocked']
    platform.import_candidates([Candidate(url='123456')])
    assert c['blocked']
    platform.resume_candidate('123456')
    assert not c['blocked'] and c['attempts'] == 0 and c['live_status'] is None
    platform.store.close()


async def test_unknown_does_not_confirm_offline_and_refusal_persists(tmp_path, monkeypatch):
    platform = fixture(tmp_path)
    platform.monitor.browser = object()
    platform.import_candidates([Candidate(url='123456')])
    platform.configure(Policy(mode='fixed', enabled=True, target_rooms=1))
    c = next(iter(platform.candidates.values()))
    await status(platform, monkeypatch, 0)
    await status(platform, monkeypatch, TimeoutError('network'))
    assert c['offline_checks'] == 0 and not c['blocked']
    await status(platform, monkeypatch, RoomStatusRefused('HTTP 429'))
    assert Platform(platform.monitor).status_refused == 'HTTP 429'
    platform.store.close()


async def test_status_response_cannot_overwrite_new_policy(tmp_path, monkeypatch):
    platform = fixture(tmp_path)
    platform.monitor.browser = object()
    platform.import_candidates([Candidate(url='123456')])
    platform.configure(Policy(mode='fixed', enabled=True, target_rooms=1))
    async def read(*args):
        platform.stop_candidate('123456')
        return {'123456': {'room_id': 123456, 'uid': 654321, 'live_status': 1}}
    monkeypatch.setattr('bilibili_live_monitor.platform.room_statuses', read)
    await platform.refresh_statuses()
    c = next(iter(platform.candidates.values()))
    assert c['blocked'] and 'live_status' not in c
    platform.store.close()


async def test_guard_snapshot_top_three_pagination_and_partial(tmp_path):
    class Response:
        status = 200
        async def json(self):
            return {'code': 0, 'data': {'info': {'num': 3, 'page': 2},
                'top3': [{'uid': 10, 'guard_level': 1}], 'list': [{'uid': request.calls + 10, 'guard_level': 3}]}}
    class Request:
        calls = 0
        async def get(self, *args, **kwargs):
            self.calls += 1
            return Response()
    request = Request()
    async def connect():
        return SimpleNamespace(request=request)
    snapshot = await guard_snapshot(SimpleNamespace(connect=connect), '123456', '654321')
    assert snapshot['complete'] and len(snapshot['members']) == 3 and request.calls == 2
    store = Store(tmp_path/'monitor.sqlite3')
    store.guard_snapshot(snapshot)
    assert store.latest_guards('123456')['reported_total'] == 3
    store.close()


def test_identity_retains_large_uid_guard_context_and_unknown_raw(tmp_path):
    uid = str(2**63 + 123)
    info = [[0, 1, 25, 0, 1792700000000], '合成点歌', [uid, '合成观众'],
            [28, '勋章', '合成主播', 123456], [], [], [], 3]
    data = frame({'cmd': 'DANMU_MSG', 'info': info})
    decoded = messages(data)[0]
    assert decoded['sender_uid'] == uid and decoded['guard_level'] == 3
    assert decoded['medal']['room_id'] == '123456'
    unknown = frame({'cmd': 'FUTURE_EVENT', 'data': {'value': 1}})
    store = Store(tmp_path/'monitor.sqlite3')
    for m in messages(data + unknown, include_raw=True):
        store.live_message('123456', {**m, 'session_id': 'synthetic-session'})
    assert store.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 2
    assert len(store.items('123456')) == 1
    store.close()
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        item = json.loads(client.get('/api/export?room_id=123456').text)
        assert item['sender_uid'] == uid and item['session_id'] == 'synthetic-session'
    info[2][0] = 0
    assert messages(frame({'cmd': 'DANMU_MSG', 'info': info}))[0]['identity_status'] == 'unresolved'


def test_resume_api_requires_existing_candidate(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.post('/api/platform/candidates/123456/resume').status_code == 404
        client.post('/api/platform/candidates', json={'candidates': [{'url': '123456'}]})
        client.post('/api/platform/candidates/123456/stop')
        assert client.post('/api/platform/candidates/123456/resume').status_code == 202
        assert not client.get('/api/platform').json()['candidates'][0]['blocked']


async def test_short_alias_fallback_block_and_canonical_resume(tmp_path, monkeypatch):
    platform = fixture(tmp_path)
    platform.monitor.browser = object()
    platform.import_candidates([Candidate(url='42'), Candidate(url='123456')])
    platform.configure(Policy(mode='fixed', enabled=True, target_rooms=2))
    short = platform.candidates['https://live.bilibili.com/42']
    long = platform.candidates['https://live.bilibili.com/123456']
    short['room_id'] = '123456'
    await status(platform, monkeypatch, 0)
    assert short['live_status'] == 0 and short['offline_checks'] == 1
    short.update(blocked=True, last_error='recording_refused: HTTP 403')
    await status(platform, monkeypatch, 1)
    assert long['blocked_alias'] and long['watch_state'] == 'attention'
    platform.resume_candidate('123456')
    assert not short['blocked'] and not long['blocked']
    assert platform.resolve_room_id('42') == '123456'
    platform.stop_candidate('123456')
    assert short['blocked'] and long['blocked']
    platform.store.close()


async def test_502_retries_and_does_not_persist_refusal(tmp_path, monkeypatch):
    platform = fixture(tmp_path)
    platform.monitor.browser = object()
    platform.import_candidates([Candidate(url='123456')])
    platform.configure(Policy(mode='fixed', enabled=True, target_rooms=1))
    class Response:
        status = 502
    class Request:
        calls = 0
        async def get(self, *args, **kwargs):
            self.calls += 1
            return Response()
    request = Request()
    async def read(*args):
        return await inspect(SimpleNamespace(request=request), [{'url': '123456'}])
    monkeypatch.setattr('bilibili_live_monitor.platform.room_statuses', read)
    await platform.refresh_statuses()
    await platform.refresh_statuses()
    assert request.calls == 2 and platform.status_refused is None
    assert next(iter(platform.candidates.values()))['watch_state'] == 'unknown'
    platform.store.close()


async def test_fixed_does_not_replace_with_unpinned_candidate(tmp_path, monkeypatch):
    platform = fixture(tmp_path)
    platform.import_candidates([Candidate(url='111'), Candidate(url='222')])
    platform.configure(Policy(mode='fixed', enabled=True, target_rooms=1))
    assert platform.policy.fixed_rooms == ['https://live.bilibili.com/111']
    platform.next_status_check = float('inf')
    async def no_guards():
        pass
    platform.refresh_guards = no_guards
    a, b = platform.candidates.values()
    a['live_status'] = b['live_status'] = 1
    await settled(platform)
    assert list(platform.active.values()) == ['111']
    a['offline_checks'] = 2
    a['live_status'] = 0
    await settled(platform)
    assert not platform.active and not b['selected']
    await platform.close()
    platform.store.close()


async def test_guard_snapshot_retains_unresolved_and_retries_502():
    class Response:
        status = 200
        async def json(self):
            return {'code': 0, 'data': {'info': {'num': 1, 'page': 1}, 'top3': [],
                                      'list': [{'uid': 0}, {'uid': 10}]}}
    response = Response()
    async def get(*args, **kwargs):
        return response
    async def connect():
        return SimpleNamespace(request=SimpleNamespace(get=get))
    snapshot = await guard_snapshot(SimpleNamespace(connect=connect), '123456', '654321')
    assert not snapshot['complete'] and snapshot['unresolved_count'] == 1
    assert snapshot['members'][-1]['identity_status'] == 'unresolved'
    response.status = 502
    try:
        await guard_snapshot(SimpleNamespace(connect=connect), '123456', '654321')
    except Exception as error:
        assert isinstance(error, RuntimeError) and not isinstance(error, RoomStatusRefused)
    else:
        raise AssertionError('502应报告暂时失败')


def test_guard_api_resolves_alias_and_omits_raw_pages(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        def seed():
            platform = client.app.state.platform
            platform.import_candidates([Candidate(url='42')])
            platform.candidates['https://live.bilibili.com/42']['room_id'] = '123456'
            platform.store.guard_snapshot({'room_id': '123456', 'observed_utc': 'synthetic',
                                          'members': [], 'raw_pages': [{'private': 'raw'}]})
        client.portal.call(seed)
        result = client.get('/api/rooms/42/guards').json()
        assert result['room_id'] == '123456' and 'raw_pages' not in result


async def test_unresolved_stopped_room_does_not_freeze_other_fixed_room(tmp_path, monkeypatch):
    platform = fixture(tmp_path)
    platform.monitor.browser = object()
    platform.import_candidates([Candidate(url='111'), Candidate(url='222'), Candidate(url='999')])
    platform.configure(Policy(mode='fixed', fixed_rooms=['111', '222'], enabled=True, target_rooms=2))
    platform.stop_candidate('222')
    platform.stop_candidate('999')
    async def read(*args):
        return {'111': {'room_id': 111, 'uid': 123, 'live_status': 1}}
    async def no_guards():
        pass
    monkeypatch.setattr('bilibili_live_monitor.platform.room_statuses', read)
    platform.refresh_guards = no_guards
    platform.next_status_check = float('inf')
    await platform.refresh_statuses()
    await settled(platform)
    assert list(platform.active.values()) == ['111']
    assert not platform.candidates['https://live.bilibili.com/111']['blocked_alias']
    assert platform.candidates['https://live.bilibili.com/222']['blocked']
    await platform.close()
    platform.store.close()


async def test_unresolved_stopped_alias_only_blocks_its_canonical_room(tmp_path, monkeypatch):
    platform = fixture(tmp_path)
    platform.monitor.browser = object()
    platform.import_candidates([Candidate(url='42'), Candidate(url='123456')])
    platform.configure(Policy(mode='fixed', fixed_rooms=['123456'], enabled=True, target_rooms=1))
    platform.stop_candidate('42')
    async def read(browser, candidates):
        assert any(c['url'].endswith('/42') for c in candidates)
        row = {'room_id': 123456, 'short_id': 42, 'uid': 123, 'live_status': 1}
        return {'42': row, '123456': row}
    monkeypatch.setattr('bilibili_live_monitor.platform.room_statuses', read)
    await platform.refresh_statuses()
    assert platform.candidates['https://live.bilibili.com/123456']['blocked_alias']
    assert platform.candidates['https://live.bilibili.com/42']['room_id'] == '123456'
    platform.store.close()
