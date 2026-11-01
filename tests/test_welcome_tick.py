import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bilibili_live_monitor.welcome import WelcomeDecision, run_welcome


def test_tick_claims_once_at_cooldown_boundary_and_keeps_seen():
    decision = WelcomeDecision({'old'}, minimum=2, window=30, cooldown=300, last_sent=0)
    assert decision.observe('a', '甲', 290, 290) is None
    assert decision.observe('b', '乙', 295, 295) is None
    assert decision.tick(299) is None
    assert [entry[1] for entry in decision.tick(300)] == ['a', 'b']
    assert decision.tick(300) is None
    assert decision.observe('c', '丙', 300, 300) is None
    decision.sent(300)
    assert decision.observe('a', '重入', 590, 590) is None
    assert decision.observe('old', '旧人', 590, 590) is None
    assert decision.observe('c', '丙', 590, 590) is None
    assert decision.observe('d', '丁', 595, 595) is None
    assert [entry[1] for entry in decision.tick(600)] == ['c', 'd']
    assert decision.tick(900) is None


@pytest.mark.parametrize('received,expected', [([270, 295], True), ([269, 295], False),
                                               ([290, 250], False)])
def test_tick_expires_every_candidate_including_out_of_order(received, expected):
    decision = WelcomeDecision(set(), minimum=2, window=30, cooldown=300, last_sent=0)
    for uid, timestamp in enumerate(received):
        assert decision.observe(str(uid), '访客', timestamp, 295) is None
    assert bool(decision.tick(300)) is expected
    if not expected:
        assert len(decision.pending) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario,continuous,expected_times', [
    ('normal', False, [300]),
    ('normal', True, [300]),
    ('multiple_batches', True, [300, 600, 900]),
    ('expired', True, []),
    ('too_few', True, []),
    ('old_and_duplicate', True, []),
    ('paused', True, []),
    ('ended', True, []),
    ('ends_before_send', True, []),
    ('failure', True, [300]),
])
async def test_tick_execution_chain(tmp_path, monkeypatch, scenario, continuous, expected_times):
    """真实增量SQL与循环，只有浏览器/发送和时钟用替身；300秒没有新报文。"""
    import bilibili_live_monitor.welcome as welcome

    path, ledger = tmp_path/'data.sqlite', tmp_path/'state.json'
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE timeline(room_id TEXT,data TEXT); '
                     'CREATE TABLE raw_events(id INTEGER PRIMARY KEY,room_id TEXT,received_utc TEXT,data TEXT);')
    base = datetime.now(timezone.utc).timestamp()-1000
    clock = [289]
    sent_times = []
    live_checks_at_boundary = [0]

    def insert(uid, received, kind='entry'):
        packet = ({'cmd': 'INTERACT_WORD', 'data': {'uid': uid, 'uname': f'访客{uid}', 'msg_type': 1}}
                  if kind == 'entry' else {'cmd': 'LIKE_INFO_V3_CLICK', 'data': {'uid': uid}})
        db.execute('INSERT INTO raw_events(room_id,received_utc,data) VALUES(?,?,?)',
                   ('123456', datetime.fromtimestamp(base+received, timezone.utc).isoformat(), json.dumps(packet)))
        db.commit()

    insert(99, 280)  # 本场基线中的旧UID不能再次入场凑人数。
    if scenario == 'expired':
        schedule = {290: [(1, 269)], 295: [(2, 295)]}
    elif scenario == 'too_few':
        schedule = {295: [(1, 295)]}
    elif scenario == 'old_and_duplicate':
        schedule = {290: [(1, 290), (99, 290)], 295: [(1, 295), (2, 295, 'like'), (2, 295)]}
    else:
        schedule = {290: [(1, 290)], 295: [(2, 295)]}
    if scenario == 'multiple_batches':
        schedule.update({590: [(1, 590), (3, 590)], 595: [(4, 595)],
                         890: [(3, 890), (5, 890)], 895: [(6, 895)]})
    end = 902 if scenario == 'multiple_batches' else 332

    async def sleep(seconds):
        clock[0] += seconds
        assert clock[0] <= end+1, '循环未按预期结束'
        for event in schedule.get(clock[0], []):
            insert(*event)

    async def activity():
        if clock[0] >= end or (scenario == 'ended' and clock[0] >= 300):
            return 'ended'
        if scenario == 'paused' and 300 <= clock[0] < 331:
            return 'paused'
        if scenario == 'ends_before_send' and clock[0] == 300:
            live_checks_at_boundary[0] += 1
            return 'live' if live_checks_at_boundary[0] == 1 else 'ended'
        return 'live'

    async def send(*args, **kwargs):
        sent_times.append(clock[0])
        if scenario == 'failure':
            raise TimeoutError('synthetic failure')
        return {'code': 0}

    page = SimpleNamespace(url='https://live.bilibili.com/123456',
                           locator=lambda _: SimpleNamespace(evaluate_all=AsyncMock(return_value=[200])))

    class PlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(
                return_value=SimpleNamespace(contexts=[SimpleNamespace(pages=[page])]))))

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(welcome, 'async_playwright', PlaywrightContext)
    monkeypatch.setattr(welcome, 'send_chat', send)
    monkeypatch.setattr(welcome.time, 'time', lambda: base+clock[0])
    monkeypatch.setattr(welcome.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(welcome.asyncio, 'sleep', sleep)
    kwargs = dict(since=datetime.fromtimestamp(base, timezone.utc).isoformat(), execute=True,
                  minimum=2, window=30, cooldown=300, last_sent=base, wait_seconds=50,
                  continuous=continuous, live_check=activity)
    try:
        if scenario == 'failure':
            with pytest.raises(TimeoutError, match='synthetic failure'):
                await run_welcome(path, '123456', 'fake', ledger, **kwargs)
            result = json.loads(ledger.read_text())
            assert result['status'] == 'ambiguous'
        else:
            result = await run_welcome(path, '123456', 'fake', ledger, **kwargs)
            assert result['status'] == ('sent' if not continuous else
                                        'stopped' if scenario == 'ends_before_send' else 'completed')
        assert sent_times == expected_times
        if expected_times and scenario != 'failure':
            batches = result['batches'] if continuous else [result]
            assert [batch['target_uids'] for batch in batches] == [
                [str(i), str(i+1)] for i in range(1, len(expected_times)*2, 2)]
            # 触发时没有第三个报文：记录最后消费的raw游标，保持原账本字段兼容。
            assert batches[0]['trigger_raw_id'] == 3
        with pytest.raises(RuntimeError, match='禁止自动重新启动|禁止重复'):
            await run_welcome(path, '123456', 'fake', ledger, **kwargs)
    finally:
        db.close()
