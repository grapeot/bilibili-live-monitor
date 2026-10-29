from copy import deepcopy
from types import SimpleNamespace

import pytest

from bilibili_live_monitor import rotation
from bilibili_live_monitor.rotation import index_rooms, plan


def test_alias_preserves_selected_room_and_unknown_is_not_stopped():
    statuses = index_rooms({'code': 0, 'data': {'by_room_ids': {
        '123456': {'room_id': 123456, 'short_id': 42, 'live_status': 1},
        '200': {'room_id': 200, 'live_status': 0},
        '300': {'room_id': 300, 'live_status': 2}}}})
    candidates = [{'url': '123456'}, {'url': '42', 'selected': True}, {'url': '200'},
                  {'url': '300'}, {'url': '400'}, {'url': '500', 'blocked': True}]
    result = plan(candidates, statuses)
    assert result == {'live': ['https://live.bilibili.com/42'],
                      'stop': ['https://live.bilibili.com/200', 'https://live.bilibili.com/300'],
                      'unknown': ['https://live.bilibili.com/400']}


@pytest.mark.parametrize('payload', [{'code': -412}, {'code': 0, 'data': {}}, {'code': 0, 'data': {'by_room_ids': []}}])
def test_refused_or_invalid_status_response_aborts(payload):
    with pytest.raises(RuntimeError):
        index_rooms(payload)


def test_blocked_alias_cannot_be_reimported_under_canonical_id():
    statuses = index_rooms({'code': 0, 'data': {'by_room_ids': {
        '123456': {'room_id': 123456, 'short_id': 42, 'live_status': 1}}}})
    assert plan([{'url': '42', 'blocked': True}, {'url': '123456'}], statuses)['live'] == []


def test_saved_canonical_id_blocks_alias_when_api_drops_short_id():
    statuses = {'123456': {'room_id': 123456, 'live_status': 1}}
    assert plan([{'url': '42', 'blocked': True, 'room_id': '123456'},
                 {'url': '123456'}], statuses)['live'] == []


def test_unresolved_block_does_not_admit_new_aliases():
    statuses = {'123456': {'room_id': 123456, 'live_status': 1}}
    assert plan([{'url': '42', 'blocked': True}, {'url': '123456'}], statuses)['live'] == []


@pytest.fixture
def execution(monkeypatch):
    state = {'capacity': 50, 'revision': 1, 'policy': {'enabled': True, 'target_rooms': 50},
             'discovery_blocked': False, 'candidates': [
                 {'url': 'https://live.bilibili.com/100', 'selected': True},
                 {'url': 'https://live.bilibili.com/200', 'selected': True}]}
    writes = []

    class Control:
        change_during_import = False

        def __init__(self, base):
            pass

        def call(self, path, data=None, method=None):
            if path == 'platform':
                return deepcopy(state)
            writes.append((path, data))
            if path == 'platform/candidates' and self.change_during_import:
                state['revision'] += 1
                state['policy']['target_rooms'] = 25
            elif path == 'platform/decisions':
                if data['revision'] != state['revision']:
                    raise RuntimeError('409 stale decision')
                state['revision'] += 1
            return {'accepted': True}

    class Browser:
        def __init__(self, url):
            pass

        async def connect(self):
            return object()

        async def discover(self, limit):
            return [{'url': 'https://live.bilibili.com/900'},
                    {'url': 'https://live.bilibili.com/1001'}]

        async def close(self):
            pass

    async def inspect(context, candidates):
        return {c['url'].rsplit('/', 1)[-1]: {'room_id': int(c['url'].rsplit('/', 1)[-1]),
                'live_status': 0 if c['url'].endswith(('/200', '/900')) else 1} for c in candidates}

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(rotation, 'Control', Control)
    monkeypatch.setattr(rotation, 'Browser', Browser)
    monkeypatch.setattr(rotation, 'inspect', inspect)
    monkeypatch.setattr(rotation.asyncio, 'sleep', no_sleep)
    return SimpleNamespace(state=state, writes=writes, control=Control,
                           args=SimpleNamespace(base_url='http://localhost', cdp_url='http://localhost',
                                                target=50, execute=True))


@pytest.mark.asyncio
async def test_execute_imports_only_live_and_stops_only_existing_atomically(execution):
    result = await rotation.rotate(execution.args)
    assert result['stop'] == ['https://live.bilibili.com/200']
    assert [p for p, _ in execution.writes] == ['platform/candidates', 'platform/decisions']
    assert execution.writes[0][1]['candidates'] == [
        {'url': 'https://live.bilibili.com/1001', 'title': '', 'priority': 500}]
    assert execution.writes[1][1]['stop'] == ['https://live.bilibili.com/200']
    assert execution.state['policy']['target_rooms'] == 50


@pytest.mark.asyncio
async def test_policy_race_rejects_single_stop_decision_without_policy_put(execution):
    execution.control.change_during_import = True
    with pytest.raises(RuntimeError, match='409'):
        await rotation.rotate(execution.args)
    assert execution.state['policy']['target_rooms'] == 25
    assert all('/stop' not in path and path != 'platform/policy' for path, _ in execution.writes)


@pytest.mark.asyncio
async def test_dry_run_has_no_control_writes(execution):
    execution.args.execute = False
    await rotation.rotate(execution.args)
    assert execution.writes == []


@pytest.mark.asyncio
async def test_target_mismatch_cannot_shrink_active_pool(execution):
    execution.args.target = 25
    with pytest.raises(ValueError, match='不修改目标'):
        await rotation.rotate(execution.args)
    assert execution.writes == []


@pytest.mark.asyncio
async def test_status_refusal_aborts_before_writes(execution, monkeypatch):
    async def refused(context, candidates):
        raise rotation.RoomStatusRefused('HTTP 412')

    monkeypatch.setattr(rotation, 'inspect', refused)
    with pytest.raises(rotation.RoomStatusRefused):
        await rotation.rotate(execution.args)
    assert execution.writes == []
