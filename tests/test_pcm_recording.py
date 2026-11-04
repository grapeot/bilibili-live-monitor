import asyncio
import base64
import json
import subprocess
import wave
from types import SimpleNamespace

import pytest

from bilibili_live_monitor import monitor as module
from bilibili_live_monitor.capture import READ_PCM
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.monitor import Monitor
from bilibili_live_monitor.pcm_recording import PCMRecording
from bilibili_live_monitor.session_offline import recording_files
from bilibili_live_monitor.store import Store


async def test_pcm_segments_are_readable_while_open_and_exact_after_close(tmp_path):
    room = {'room_id': '123456'}
    rec = PCMRecording(Settings(data_dir=tmp_path, recording_segment_seconds=10), room)
    pcm = b'\x01\x02' * 400000
    rec.write(pcm)
    files = sorted(rec.directory.glob('*.wav'))
    assert len(files) == 3
    chunks = []
    for path in files:
        with wave.open(str(path)) as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
            chunks.append(wav.readframes(wav.getnframes()))
    assert b''.join(chunks) == pcm
    assert rec.snapshot()['recorded_seconds'] == 25
    await rec.close()
    await rec.close()
    assert rec.snapshot()['recording_bytes'] == sum(p.stat().st_size for p in files)
    cfg = {'recording_root': str(rec.directory), 'start_utc': '2020-01-01T00:00:00+00:00'}
    assert sum(len(group) for group in recording_files(cfg, 4102444800).values()) == 3


async def test_reconnect_does_not_overwrite_previous_audio_and_reports_stall(tmp_path):
    settings, room = Settings(data_dir=tmp_path), {'room_id': '123456'}
    first = PCMRecording(settings, room)
    first.write(b'\x01\x02' * 16000)
    await first.close()
    room.update(first.snapshot())
    second = PCMRecording(settings, room)
    second.write(b'\x03\x04' * 16000)
    assert second.snapshot()['recorded_seconds'] == 2
    assert second.snapshot()['recording_files'] == 2
    second.last_progress -= 41
    with pytest.raises(RuntimeError, match='recording_stalled'):
        await second.check()
    await second.close()
    assert first.prefix != second.prefix


@pytest.mark.parametrize('asr', [False, True])
@pytest.mark.parametrize('invalid_source', [False, True])
async def test_audio_only_capture_uses_web_pcm_without_media_url(tmp_path, monkeypatch, asr, invalid_source):
    store = Store(tmp_path / 'db.sqlite3')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    room = {'room_id': '123456', 'record_mode': 'audio', 'asr_enabled': asr}
    monitor.failures['123456'] = 0
    page = SimpleNamespace(is_closed=lambda: False)
    got_audio = asyncio.Event()
    submitted = []
    async def inspect(*args, **kwargs):
        assert not kwargs.get('pause')
    async def noop(*args):
        pass
    async def read(*args):
        if args[1] == 0:
            return b'\x03\x04' * 32000, {}
        if got_audio.is_set():
            await asyncio.sleep(.01)
            return b'', {}
        got_audio.set()
        return b'\x01\x02' * 80000, {'started': 0, 'ended': 5000, 'muted': False,
            'paused': False, 'track_live': True, 'media_advanced': 5, 'pauses': 0,
            'missing_samples': 0, 'dropped_samples': 0, 'track_muted_ms': 0}
    monkeypatch.setattr(module, 'start_pcm', noop)
    monkeypatch.setattr(module, 'close_pcm', noop)
    monkeypatch.setattr(module, 'read_pcm', read)
    def validate(*args):
        if invalid_source:
            raise RuntimeError('synthetic paused input')
    monkeypatch.setattr(module, 'validate_source', validate)
    monkeypatch.setattr(monitor, 'inspect_page', inspect)
    monkeypatch.setattr(monitor, 'submit', lambda *args: submitted.append(args))
    monkeypatch.setattr(monitor, 'finish_stream', lambda *args: None)
    task = asyncio.create_task(monitor.session_cycles(room, page, 'generation'))
    await asyncio.wait_for(got_audio.wait(), 2)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert room['recorded_seconds'] == 7
    assert room['recording_status'] == 'stopped'
    assert len(submitted) == int(asr and not invalid_source)
    with wave.open(str(next((tmp_path / 'recordings' / '123456').glob('*.wav')))) as wav:
        assert wav.getnframes() == 112000
    store.close()


async def test_mixed_session_statistics_and_close_failure_release_file(tmp_path):
    from bilibili_live_monitor.recording import Recording
    rec = PCMRecording(Settings(data_dir=tmp_path), {'room_id': '123456'})
    rec.write(b'\0\0' * 16000)
    (rec.directory / f'{rec.session}-old.mka').write_bytes(b'x' * 1000)
    assert rec.snapshot()['recording_files'] == 2
    assert rec.snapshot()['recording_bytes'] == 33044
    video = Recording(Settings(data_dir=tmp_path), {'room_id': '123456', 'recording_session': rec.session})
    assert video.snapshot()['recording_bytes'] == 33044
    assert video.snapshot()['recording_files'] == 2
    writer = rec.writer
    original_close = writer.close
    def broken():
        original_close()
        raise OSError('synthetic header error')
    rec.writer.close = broken
    file = rec.file
    with pytest.raises(OSError):
        await rec.close()
    writer.close = original_close
    assert file.closed and rec.closed and rec.writer is None


@pytest.mark.parametrize('asr', [False, True])
async def test_replaced_player_returns_buffer_before_reconnect(tmp_path, monkeypatch, asr):
    script = 'const read = ' + READ_PCM + ''';
const video={currentTime:5,muted:true,paused:false};
const m={video,samples:80000,parts:[new Float32Array(80000).fill(0.25)],
  flushes:new Map(),context:{sampleRate:16000,state:'running'},last:Date.now()-5000,
  lastMedia:0,tracks:[{readyState:'live',enabled:true,muted:false}],
  mutedMs:0,mutedAt:null,missing:0,dropped:0,pauses:0,playError:null};
m.tap={port:{postMessage:({id})=>m.flushes.get(id)()}};
global.window={__pcmMonitor:m};
global.document={querySelector:()=>({currentTime:0})};
read(0).then(result=>console.log(JSON.stringify({result,remaining:m.samples})));
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True, timeout=10)
    output = json.loads(result.stdout)
    payload = output['result']
    pcm = base64.b64decode(payload.pop('data'))
    assert len(pcm) == 160000 and output['remaining'] == 0 and payload['source_changed']
    store = Store(tmp_path / 'source.sqlite3')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    recorder = PCMRecording(monitor.settings, {'room_id': '123456'})
    async def noop(*args):
        pass
    async def read(page, seconds):
        return (pcm, payload) if seconds else (b'', {})
    monkeypatch.setattr(module, 'start_pcm', noop)
    monkeypatch.setattr(module, 'close_pcm', noop)
    monkeypatch.setattr(module, 'read_pcm', read)
    with pytest.raises(RuntimeError, match='播放器元素已更换'):
        await monitor.capture_cycles({'room_id': '123456', 'record_mode': 'audio', 'asr_enabled': asr},
                                     SimpleNamespace(is_closed=lambda: False), 'synthetic', recorder)
    await recorder.close()
    with wave.open(str(next(recorder.directory.glob('*.wav')))) as audio:
        assert audio.getnframes() == 80000
        assert audio.readframes(80000) == pcm
    assert monitor.queue.empty()
    store.close()


async def test_failed_read_still_drains_tail(tmp_path, monkeypatch):
    store = Store(tmp_path / 'source.sqlite3')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    recorder = PCMRecording(monitor.settings, {'room_id': '123456'})
    async def noop(*args):
        pass
    async def read(page, seconds):
        if seconds:
            raise RuntimeError('synthetic flush failure')
        return b'\x01\x02' * 16000, {}
    monkeypatch.setattr(module, 'start_pcm', noop)
    monkeypatch.setattr(module, 'close_pcm', noop)
    monkeypatch.setattr(module, 'read_pcm', read)
    with pytest.raises(RuntimeError, match='synthetic flush failure'):
        await monitor.capture_cycles({'room_id': '123456', 'record_mode': 'audio', 'asr_enabled': False},
                                     SimpleNamespace(is_closed=lambda: False), 'synthetic', recorder)
    await recorder.close()
    with wave.open(str(next(recorder.directory.glob('*.wav')))) as audio:
        assert audio.getnframes() == 16000
    store.close()
