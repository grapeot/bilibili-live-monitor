import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bilibili_live_monitor.welcome import WelcomeDecision, run_welcome
from bilibili_live_monitor.welcome_session import live_state


def snapshot(status=1, checks=0, now=1000):
    return {'policy': {'enabled': True, 'mode': 'fixed', 'fixed_rooms': ['https://live.bilibili.com/123456'], 'target_rooms': 1},
            'candidates': [{'room_id': '123456', 'live_status': status, 'offline_checks': checks,
                            'status_checked_utc': datetime.fromtimestamp(now, timezone.utc).isoformat()}]}


@pytest.mark.parametrize(('status', 'checks', 'expected'), [(1, 0, 'live'), (0, 1, 'paused'),
                                                         (0, 2, 'ended'), (2, 2, 'ended'), (None, 0, 'paused')])
def test_live_state_only_confirms_two_offline_probes(status, checks, expected):
    assert live_state(snapshot(status, checks), '123456', now=1000) == expected


def test_stale_unknown_and_scope_changes():
    assert live_state(snapshot(), '123456', now=1100) == 'paused'
    data = snapshot()
    data['candidates'][0]['status_error'] = 'network unavailable'
    assert live_state(data, '123456', now=1000) == 'paused'
    data = snapshot()
    data['policy']['enabled'] = False
    assert live_state(data, '123456', now=1000) == 'ended'
    data = snapshot()
    data['policy']['fixed_rooms'].append('654321')
    assert live_state(data, '123456', now=1000) == 'ended'


def test_repeat_cooldown_and_seen_users():
    decision = WelcomeDecision(set(), minimum=2, cooldown=300)
    assert decision.observe('1', 'a', 0, 0) is None
    assert decision.observe('2', 'b', 1, 1)
    decision.sent(2)
    assert decision.observe('1', 'old', 302, 302) is None
    assert decision.observe('3', 'c', 280, 280) is None
    assert decision.observe('4', 'd', 290, 290) is None
    assert decision.observe('5', 'e', 302, 302)
    decision.sent(302)
    assert not decision.pending and decision.last_sent == 302


@pytest.mark.asyncio
async def test_continuous_two_batches_then_permanent_end(tmp_path, monkeypatch):
    import bilibili_live_monitor.welcome as welcome
    path, ledger = tmp_path/'data.sqlite', tmp_path/'session.json'
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE timeline(room_id TEXT,data TEXT); CREATE TABLE raw_events(id INTEGER PRIMARY KEY,room_id TEXT,received_utc TEXT,data TEXT);')
    clock = [datetime.now(timezone.utc).timestamp()]
    inserted = set()
    sender = AsyncMock(return_value={'code': 0})
    async def activity():
        count = sender.await_count
        if count >= 4:
            return 'ended'
        if count in (0, 2) and count not in inserted:
            inserted.add(count)
            if count == 2:
                clock[0] += 301
            for uid in (count+1, count+2):
                packet = {'cmd': 'INTERACT_WORD', 'data': {'uid': uid, 'uname': f'合成{uid}', 'msg_type': 1}}
                db.execute('INSERT INTO raw_events(room_id,received_utc,data) VALUES(?,?,?)',
                           ('123456', datetime.fromtimestamp(clock[0], timezone.utc).isoformat(), json.dumps(packet)))
            db.commit()
        return 'live'
    page = SimpleNamespace(url='https://live.bilibili.com/123456',
                           locator=lambda _: SimpleNamespace(evaluate_all=AsyncMock(return_value=[80])))
    class PlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(
                return_value=SimpleNamespace(contexts=[SimpleNamespace(pages=[page])]))))
        async def __aexit__(self, *_):
            pass
    monkeypatch.setattr(welcome, 'async_playwright', PlaywrightContext)
    monkeypatch.setattr(welcome, 'send_chat', sender)
    monkeypatch.setattr(welcome.time, 'time', lambda: clock[0])
    monkeypatch.setattr(welcome.asyncio, 'sleep', AsyncMock())
    result = await run_welcome(path, '123456', 'fake', ledger, since='2026-01-01T00:00:00Z',
                               continuous=True, execute=True, minimum=2, cooldown=300, live_check=activity)
    assert result['status'] == 'completed'
    assert len(result['batches']) == 2 and sender.await_count == 4
    assert result['batches'][1]['sent_at']-result['batches'][0]['sent_at'] >= 300
    with pytest.raises(RuntimeError, match='禁止自动重新启动'):
        await run_welcome(path, '123456', 'fake', ledger, since='2026-01-01T00:00:00Z',
                           continuous=True, execute=True, live_check=activity)
    db.close()
