import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from bilibili_live_monitor.app import create_app, sse
from bilibili_live_monitor.browser import decode_webm, room_url
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.monitor import Monitor, rms_dbfs, window_metadata
from bilibili_live_monitor.store import Store


def test_room_url():
    assert room_url('123456') == room_url('https://live.bilibili.com/123456?live_from=example')
    for value in ['https://example.com/123456', '0', 'javascript:alert(1)', '//live.bilibili.com/2']:
        with pytest.raises(ValueError):
            room_url(value)


def test_missing_audio_does_not_look_like_complete_window():
    metadata = window_metadata(bytes(32000*30), 1000, 31000)
    assert metadata['coverage_ratio'] == 1
    assert metadata['wall_seconds'] == 30
    with pytest.raises(RuntimeError, match='覆盖异常'):
        window_metadata(bytes(32000*5), 1000, 31000)


async def test_pending_play_is_bounded(monkeypatch):
    from bilibili_live_monitor import browser
    real_wait_for = asyncio.wait_for

    class Page:
        async def evaluate(self, *args):
            await asyncio.Future()

    async def short_timeout(awaitable, timeout):
        assert timeout == 10
        return await real_wait_for(awaitable, 0.01)

    monkeypatch.setattr(browser.asyncio, 'wait_for', short_timeout)
    with pytest.raises(TimeoutError):
        await browser.ensure_playing(Page())


def test_api_validation(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        assert client.get('/api/health').json()['ok']
        assert client.get('/api/state').json()['rooms'] == []
        assert client.post('/api/rooms', json={'url': '123456', 'chunk_seconds': 4}).status_code == 422
        assert client.post('/api/rooms', json={'url': 'invalid'}).status_code == 422
        assert client.post('/api/rooms/123456/stop').status_code == 404
        schema = client.get('/openapi.json').json()
        assert '201' in schema['paths']['/api/rooms']['post']['responses']


def test_prefix_and_crash_recovery(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, root_path='/example'))
    with TestClient(app) as client:
        assert client.get('/example/api/health').status_code == 200
    store = Store(tmp_path / 'monitor.sqlite3')
    store.segment({'room_id': '123456', 'seq': 1, 'status': 'queued'})
    store.close()
    store = Store(tmp_path / 'monitor.sqlite3')
    assert store.segments('123456')[0]['status'] == 'cancelled'
    store.close()


def test_history_and_event_replay(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path)
    store.room({'room_id': '123456', 'status': 'capturing'})
    cursor = store.cursor()
    for seq in range(1, 25):
        store.segment({'room_id': '123456', 'seq': seq, 'text': f'合成文本 {seq}', 'status': 'done'})
    events = store.events(cursor)
    assert len(events) == 48
    assert [e['kind'] for e in events[:2]] == ['segment', 'item']
    assert all(e['id'] > cursor for e in events)
    frame = sse(events[0]['kind'], json.loads(events[0]['data']), events[0]['id'])
    assert frame.startswith(f'id: {cursor + 1}\nevent: segment\n')
    store.close()
    restored = Store(path)
    assert restored.rooms()[0]['status'] == 'stopped'
    assert [r['seq'] for r in restored.segments('123456')] == list(range(5, 25))
    assert restored.next_seq('123456') == 25
    restored.close()


async def test_silence_overload_and_shared_worker(tmp_path):
    settings = Settings(data_dir=tmp_path, queue_size=1)
    store = Store(tmp_path / 'monitor.db')
    monitor = Monitor(settings, store)
    monitor.generations['123456'] = 'generation'
    base = {'room_id': '123456', 'text': '', 'infer_ms': 0}
    silent = bytes(32000)
    monitor.submit({**base, 'seq': 1, 'rms_dbfs': rms_dbfs(silent)}, silent, 'generation')
    assert monitor.queue.empty()
    assert store.segments('123456')[0]['status'] == 'silent'
    loud = (np.ones(16000, dtype='<i2') * 1000).tobytes()
    for seq in (2, 3):
        monitor.submit({**base, 'seq': seq, 'rms_dbfs': rms_dbfs(loud)}, loud, 'generation')
    assert monitor.queue.qsize() == 1
    assert monitor.overload_count == 1
    assert store.segments('123456')[1]['status'] == 'dropped'
    assert store.segments('123456')[-1]['status'] == 'queued'
    calls = []

    async def recognize(path, req_id):
        assert Path(path).exists()
        calls.append(req_id)
        return {'text': '合成结果', 'infer_ms': 5}

    monitor.asr.transcribe = recognize
    await monitor.start(warm=False)
    await asyncio.wait_for(monitor.queue.join(), 3)
    assert len(calls) == 1
    assert store.segments('123456')[-1]['status'] == 'done'
    assert not list((tmp_path / 'audio').iterdir())
    await monitor.close()
    store.close()


async def test_continuous_webm_later_window():
    # 合成两个不同频率的连续音频窗口，验证累计解码后不重复第一段。
    sr = 48000
    t = np.arange(sr) / sr
    signal = np.concatenate([np.sin(2*np.pi*330*t), np.sin(2*np.pi*880*t)])
    pcm = (signal * 12000).astype('<i2').tobytes()
    process = await asyncio.create_subprocess_exec(
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 's16le', '-ar', str(sr), '-ac', '1',
        '-i', 'pipe:0', '-c:a', 'libopus', '-f', 'webm', 'pipe:1',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    encoded, error = await process.communicate(pcm)
    assert process.returncode == 0, error
    decoded = await decode_webm(encoded)
    samples = np.frombuffer(decoded, dtype='<i2')
    assert 1.95 < len(samples)/16000 < 2.05
    frequencies = []
    for part in (samples[1000:15000], samples[17000:31000]):
        peak = np.argmax(abs(np.fft.rfft(part)))
        frequencies.append(peak * 16000/len(part))
    assert abs(frequencies[0] - 330) < 3
    assert abs(frequencies[1] - 880) < 3


async def test_reconnect_after_interrupted_track(tmp_path):
    store = Store(tmp_path / 'monitor.db')
    monitor = Monitor(Settings(data_dir=tmp_path, retry_delay=0), store)
    room = {'room_id': '123456', 'status': 'capturing', 'chunk_seconds': 30, 'error': None}

    class Page:
        closed = False
        reloads = 0

        async def reload(self, **kwargs):
            self.reloads += 1

        async def wait_for_function(self, *args, **kwargs):
            pass

        async def evaluate(self, *args):
            pass

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    page = Page()
    attempts = []

    async def cycles(room, page, generation):
        attempts.append(generation)
        if len(attempts) == 1:
            raise RuntimeError('合成音轨中断')
        raise asyncio.CancelledError()

    monitor.capture_cycles = cycles
    monitor.generations['123456'] = 'generation'
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(monitor.capture(room, page, 'generation'), 5)
    assert page.reloads == 1 and page.closed
    assert len(attempts) == 2
    assert room['status'] == 'capturing' and room['error'] is None
    assert any('reconnecting' in event['data'] for event in store.events(0))
    store.close()
