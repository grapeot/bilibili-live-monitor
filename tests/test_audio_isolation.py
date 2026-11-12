import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bilibili_live_monitor import monitor as module


@pytest.mark.asyncio
async def test_audio_failure_retries_without_ending_recording(monkeypatch):
    room = {'room_id': '123456', 'record_mode': 'video'}
    store = SimpleNamespace(room=Mock(), event=Mock())
    cycles = AsyncMock(side_effect=[RuntimeError('播放器元素已更换'), asyncio.CancelledError()])
    owner = SimpleNamespace(store=store, capture_cycles=cycles, capture_recovery={})
    monkeypatch.setattr(module.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(module, 'ensure_playing', AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await module.Monitor.capture_audio(owner, room, object(), 'synthetic')
    assert cycles.await_count == 2
    assert room['audio_error'].endswith('播放器元素已更换')
    assert store.event.call_args.args[0] == 'audio_capture_retry'


@pytest.mark.asyncio
async def test_page_recovery_request_is_not_swallowed():
    from bilibili_live_monitor.capture_recovery import PageRecoveryRequired
    owner = SimpleNamespace(capture_recovery={}, capture_cycles=AsyncMock(side_effect=PageRecoveryRequired('synthetic')))
    with pytest.raises(PageRecoveryRequired, match='synthetic'):
        await module.Monitor.capture_audio(owner, {'room_id': '123456', 'record_mode': 'none'}, object(), 'synthetic')
