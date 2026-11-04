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
    owner = SimpleNamespace(store=store, capture_cycles=cycles)
    monkeypatch.setattr(module.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(module, 'ensure_playing', AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await module.Monitor.capture_audio(owner, room, object(), 'synthetic')
    assert cycles.await_count == 2
    assert room['audio_error'].endswith('播放器元素已更换')
    assert store.event.call_args.args[0] == 'audio_capture_retry'


@pytest.mark.asyncio
async def test_without_recording_original_reconnect_behavior_is_preserved():
    owner = SimpleNamespace(capture_cycles=AsyncMock(side_effect=RuntimeError('synthetic')))
    with pytest.raises(RuntimeError, match='synthetic'):
        await module.Monitor.capture_audio(owner, {'record_mode': 'none'}, object(), 'synthetic')
