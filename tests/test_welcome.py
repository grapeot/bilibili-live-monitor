import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bilibili_live_monitor.welcome import (
    INTRO,
    WelcomeDecision,
    run_once,
    send_chat,
    text_length,
    welcome_messages,
)


def test_unique_new_users_window_and_one_shot():
    decision = WelcomeDecision({'old'})
    assert decision.observe('old', 'old', 1, 1) is None
    assert decision.observe(None, 'anonymous', 1, 1) is None
    for i in range(3):
        assert decision.observe(str(i), str(i), i, i) is None
    assert decision.observe('0', 'reentry', 3, 3) is None
    assert len(decision.observe('3', 'new', 4, 4)) == 4
    assert decision.observe('4', 'another', 5, 5) is None


def test_window_expiry_and_cooldown():
    decision = WelcomeDecision(set(), minimum=2, last_sent=0)
    assert decision.observe('1', 'a', 280, 280) is None
    assert decision.observe('2', 'b', 290, 290) is None
    assert decision.observe('3', 'c', 301, 301) is not None
    decision = WelcomeDecision(set(), minimum=2)
    assert decision.observe('1', 'a', 0, 0) is None
    assert decision.observe('2', 'b', 31, 31) is None
    assert decision.observe('3', 'c', 32, 32) is not None


def test_message_length_and_split():
    assert len(welcome_messages(['合成访客'], 80)) == 1
    split = welcome_messages(['合成访客'], text_length(INTRO))
    assert len(split) == 2 and split[1] == INTRO
    assert all(text_length(s) <= text_length(INTRO) for s in split)
    assert text_length('😀') == 2
    with pytest.raises(ValueError):
        welcome_messages(['访客'], 10)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['sending', 'sent', 'ambiguous'])
async def test_existing_attempt_never_sends_again(tmp_path, status):
    state = tmp_path/'state.json'
    state.write_text(json.dumps({'status': status}))
    with pytest.raises(RuntimeError, match='禁止重复'):
        await run_once(tmp_path/'missing.sqlite', '123456', 'not-used', state, execute=True)


@pytest.mark.asyncio
async def test_dry_run_does_not_touch_chrome(tmp_path):
    path = tmp_path/'history.sqlite'
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE timeline(room_id TEXT,data TEXT); CREATE TABLE raw_events(id INTEGER,room_id TEXT,received_utc TEXT,data TEXT);')
    db.close()
    result = await run_once(path, '123456', 'invalid-url', tmp_path/'state.json')
    assert result['status'] == 'dry_run'
    assert not (tmp_path/'state.json').exists()


class ResponseContext:
    def __init__(self, code):
        self.value = AsyncMock(return_value=SimpleNamespace(status=200, json=AsyncMock(return_value={'code': code})))()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [0, -1])
async def test_send_uses_one_click_without_retry(code):
    field = SimpleNamespace(count=AsyncMock(return_value=1), input_value=AsyncMock(return_value=''), fill=AsyncMock())
    button = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock())
    page = SimpleNamespace(locator=lambda s: field if 'chat-input' in s else button,
                           expect_response=lambda *a, **k: ResponseContext(code))
    if code:
        with pytest.raises(RuntimeError):
            await send_chat(page, '合成测试')
    else:
        assert (await send_chat(page, '合成测试'))['code'] == 0
    field.fill.assert_awaited_once_with('合成测试')
    button.click.assert_awaited_once()
