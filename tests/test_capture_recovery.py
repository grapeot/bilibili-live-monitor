import asyncio
import json
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bilibili_live_monitor import monitor as module
from bilibili_live_monitor.capture_recovery import CaptureRecovery, PageRecoveryRequired
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.monitor import Monitor
from bilibili_live_monitor.pcm_recording import PCMRecording
from bilibili_live_monitor.store import Store


def test_episode_survives_rebind_and_one_good_window_but_clears_after_ten_seconds():
    now = [0]
    recovery = CaptureRecovery(lambda: now[0])
    assert recovery.bad() == 'observe'
    now[0] = 15
    assert recovery.bad() == 'rebind'
    recovery.good(5)
    now[0] = 30
    assert recovery.bad() == 'reload'
    recovery.good(5)
    recovery.good(5)
    assert recovery.bad() == 'observe'
    recovery.record('page_reloads')
    recovery.new_page()
    now[0] = 3640
    assert recovery.summary()['last_hour']['page_reloads'] == 0
    assert recovery.summary()['totals']['page_reloads'] == 1


def payload(start, media=5, **kwargs):
    return dict(started=start, ended=start+5000, muted=True, paused=False,
        track_live=True, context_state='running', missing_samples=0, dropped_samples=0,
        media_advanced=media, pauses=0, play_error=None, track_muted=False,
        track_enabled=True, track_muted_ms=0, player={'ready_state': 2}, **kwargs)


@pytest.mark.parametrize('asr', [False, True])
async def test_single_slow_window_keeps_pcm_and_splits_asr_context(tmp_path, monkeypatch, asr):
    store = Store(tmp_path / 'monitor.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    room = {'room_id': '123456', 'record_mode': 'audio', 'asr_enabled': asr}
    recorder = PCMRecording(monitor.settings, room)
    page = SimpleNamespace(is_closed=lambda: False)
    blocks = [payload(0), payload(5000, media=2), payload(10000), payload(15000)]
    submitted = []

    async def read(page, seconds):
        if not seconds:
            return b'', {}
        if not blocks:
            raise asyncio.CancelledError()
        return b'\x01\x02'*80000, blocks.pop(0)

    monkeypatch.setattr(module, 'start_pcm', AsyncMock())
    monkeypatch.setattr(module, 'close_pcm', AsyncMock())
    monkeypatch.setattr(module, 'read_pcm', read)
    monkeypatch.setattr(monitor, 'submit', lambda segment, *args: submitted.append(segment))
    with pytest.raises(asyncio.CancelledError):
        await monitor.capture_cycles(room, page, 'synthetic', recorder)
    await recorder.close()
    with wave.open(str(next(recorder.directory.glob('*.wav')))) as wav:
        assert wav.getnframes() == 320000
    assert len(submitted) == (3 if asr else 0)
    if asr:
        assert submitted[0]['stream_id'] != submitted[1]['stream_id']
        assert submitted[1]['origin_ms'] == 10000 and submitted[1]['sample_offset'] == 0
    assert room['capture_health']['totals']['invalid_seconds'] == 5
    assert room['capture_health']['totals']['valid_seconds'] == 15
    assert room['capture_health']['state'] == 'healthy'
    assert store.segments('123456')[0]['status'] == 'error'
    store.close()


async def test_persistent_stall_escalates_after_local_rebind_without_resetting_recording(tmp_path, monkeypatch):
    store = Store(tmp_path / 'monitor.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    room = {'room_id': '123456', 'record_mode': 'audio', 'asr_enabled': False}
    recorder = PCMRecording(monitor.settings, room)
    now = [0]
    monitor.capture_recovery['123456'] = CaptureRecovery(lambda: now[0])
    starts = []

    async def start(page):
        starts.append(now[0])

    async def read(page, seconds):
        if not seconds:
            return b'', {}
        now[0] += 5
        return bytes(160000), payload(now[0]*1000, media=0)

    monkeypatch.setattr(module, 'start_pcm', start)
    monkeypatch.setattr(module, 'close_pcm', AsyncMock())
    monkeypatch.setattr(module, 'read_pcm', read)
    monkeypatch.setattr(module, 'ensure_playing', AsyncMock())
    monkeypatch.setattr(module.asyncio, 'sleep', AsyncMock())
    with pytest.raises(PageRecoveryRequired):
        await monitor.capture_audio(room, SimpleNamespace(is_closed=lambda: False), 'synthetic', recorder)
    await recorder.close()
    assert starts == [0, 20]
    assert recorder.progress_seconds == 35
    assert len(list(recorder.directory.glob('*.wav'))) == 2
    assert monitor.capture_recovery['123456'].summary()['totals']['local_restarts'] == 1
    assert all(json.loads(e['data'])['status'] == 'error' for e in store.events(0) if e['kind'] == 'segment')
    store.close()


async def test_rebind_preserves_file_and_emits_wall_clock_gap(tmp_path, monkeypatch):
    store = Store(tmp_path / 'monitor.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    room = {'room_id': '123456', 'record_mode': 'audio', 'asr_enabled': False}
    recorder = PCMRecording(monitor.settings, room)
    blocks = [payload(0, source_changed=True), payload(7000)]

    async def read(page, seconds):
        if not seconds:
            return b'', {}
        if not blocks:
            raise asyncio.CancelledError()
        return bytes(160000), blocks.pop(0)

    monkeypatch.setattr(module, 'start_pcm', AsyncMock())
    monkeypatch.setattr(module, 'close_pcm', AsyncMock())
    monkeypatch.setattr(module, 'read_pcm', read)
    monkeypatch.setattr(module, 'ensure_playing', AsyncMock())
    monkeypatch.setattr(module.asyncio, 'sleep', AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await monitor.capture_audio(room, SimpleNamespace(is_closed=lambda: False), 'synthetic', recorder)
    await recorder.close()
    assert recorder.progress_seconds == 10
    assert len(list(recorder.directory.glob('*.wav'))) == 2
    gaps = [json.loads(e['data']) for e in store.events(0) if e['kind'] == 'capture_gap']
    assert len(gaps) == 1 and gaps[0]['seconds'] == 2
    store.close()
