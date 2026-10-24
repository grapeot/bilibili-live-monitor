import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bilibili_live_monitor.app import create_app
from bilibili_live_monitor.browser import Browser
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.monitor import Monitor
from bilibili_live_monitor.recording import Recording, command, is_media_url
from bilibili_live_monitor.store import Store


async def test_default_recording_does_not_start_asr_or_create_queue_worker(tmp_path):
    settings = Settings(data_dir=tmp_path)
    store = Store(tmp_path / 'monitor.db')
    monitor = Monitor(settings, store)

    async def forbidden():
        pytest.fail('禁用ASR时不得启动模型')

    monitor.asr.start = forbidden
    await monitor.start()
    assert settings.record_mode == 'audio'
    assert not monitor.metrics()['asr_enabled']
    assert monitor.worker is None and monitor.warm_task is None
    await monitor.close()
    store.close()


def test_recording_api_options_and_preview_disabled(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get('/api/health').json()['asr_enabled'] is False
        assert client.get('/api/rooms/123456/preview.jpg').status_code == 404
        assert client.post('/api/rooms', json={'url':'123456','record_mode':'invalid'}).status_code == 422


def test_copy_commands_and_url_filter(tmp_path):
    args = command('https://cdn.bilivideo.com/live.flv?token=example',
                   'https://live.bilibili.com/123456', 'audio', tmp_path/'%05d.mka', 300)
    assert args[args.index('-map')+1] == '0:a:0'
    assert '-vn' in args and args[args.index('-c')+1] == 'copy'
    assert '-segment_time' in args and '-progress' in args
    assert is_media_url('https://cdn.bilivideo.com/live.flv?token=example')
    assert not is_media_url('https://bilivideo.com.evil.example/live.flv')
    assert not is_media_url('https://cdn.bilivideo.com/segment.m4s')


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='需要ffmpeg验证真实无转码录制')
@pytest.mark.parametrize('mode,types', [('audio', ['audio']), ('video', ['video','audio'])])
async def test_real_segmented_copy_and_finalization(tmp_path, mode, types):
    source = tmp_path / 'synthetic.mkv'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=size=64x64:rate=5',
                    '-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','24',
                    '-c:v','mpeg4','-c:a','aac',str(source)], check=True, timeout=15)
    room = {'room_id':'123456','url':'https://live.bilibili.com/123456','record_mode':mode}
    recorder = Recording(Settings(data_dir=tmp_path, recording_segment_seconds=10), room)
    await recorder.start(str(source))
    await asyncio.wait_for(recorder.process.wait(), 10)
    await recorder.close()
    assert recorder.process.returncode == 0
    files = list((tmp_path/'recordings'/'123456').glob('*.mk*'))
    assert len(files) >= 2
    durations = []
    for path in files:
        data = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format',
                                                  '-of','json',str(path)]))
        assert [s['codec_type'] for s in data['streams']] == types
        assert next(s for s in data['streams'] if s['codec_type']=='audio')['codec_name'] == 'aac'
        durations.append(float(data['format']['duration']))
    assert sum(durations) >= 23.9
    assert recorder.snapshot()['recording_bytes'] == sum(p.stat().st_size for p in files)
    assert recorder.progress_seconds > 1


async def test_refusal_is_sticky_while_process_alive_and_url_redacted(tmp_path):
    recorder = Recording(Settings(data_dir=tmp_path), {'room_id':'123456'})
    reader = asyncio.StreamReader()
    reader.feed_data(b'HTTP error 429 Too Many Requests https://cdn.example/x?secret=not-public\n')
    reader.feed_data(b'other diagnostic\n'*20)
    reader.feed_eof()

    class Process:
        stderr = reader
        returncode = None

    recorder.process = Process()
    await recorder.read_errors()
    assert recorder.refused == '429'
    with pytest.raises(RuntimeError, match='recording_refused'):
        await recorder.check()
    assert all('secret=' not in line for line in recorder.error_lines)


async def test_concurrent_close_survives_cancelled_waiter(tmp_path):
    recorder = Recording(Settings(data_dir=tmp_path), {'room_id':'123456'})
    signalled, finish = asyncio.Event(), asyncio.Event()

    class Process:
        returncode = None
        signals = 0

        def send_signal(self, _):
            self.signals += 1
            signalled.set()

        async def wait(self):
            await finish.wait()
            self.returncode = 0

    process = Process()
    recorder.process = process
    first = asyncio.create_task(recorder.close())
    await signalled.wait()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    second = asyncio.create_task(recorder.close())
    finish.set()
    await second
    assert process.signals == 1 and process.returncode == 0 and recorder.closed


async def test_refusal_discovered_during_stderr_drain_remains_terminal(tmp_path):
    recorder = Recording(Settings(data_dir=tmp_path), {'room_id':'123456'})
    recorder.process = SimpleNamespace(returncode=1)

    async def late_stderr():
        await asyncio.sleep(0)
        recorder.refused = '403'
        recorder.error_lines = ['later diagnostic']*12

    recorder.tasks = [asyncio.create_task(late_stderr())]
    with pytest.raises(RuntimeError,match='recording_refused'):
        await recorder.check()


async def test_successful_media_response_refreshes_hls_and_ignores_failed_flv():
    class Page:
        url = 'https://live.bilibili.com/123456'
        handlers = {}

        def on(self, name, callback):
            self.handlers[name] = callback

        async def add_init_script(self, _):
            pass

        async def goto(self, *args, **kwargs):
            self.handlers['response'](SimpleNamespace(status=403,url='https://cdn.bilivideo.com/bad.flv'))
            self.handlers['response'](SimpleNamespace(status=200,url='https://cdn.bilivideo.com/a.m3u8?sign=a'))

        async def wait_for_function(self, *args, **kwargs):
            pass

        async def evaluate(self, *args):
            pass

    page = Page()

    class Context:
        async def new_page(self):
            return page

    browser = Browser('http://localhost:9222')

    async def connect():
        return Context()

    browser.connect = connect
    await browser.page(page.url)
    assert page.recording_url.endswith('sign=a')
    page.handlers['response'](SimpleNamespace(status=200,url='https://cdn.bilivideo.com/b.m3u8?sign=b'))
    assert page.recording_url.endswith('sign=b')


async def test_concurrent_room_stop_only_cancels_once(tmp_path):
    store = Store(tmp_path/'monitor.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def stop_once(room_id):
        calls.append(room_id)
        started.set()
        await finish.wait()
        return {'room_id':room_id}

    monitor._stop = stop_once
    first = asyncio.create_task(monitor.stop('123456'))
    await started.wait()
    second = asyncio.create_task(monitor.stop('123456'))
    await asyncio.sleep(0)
    finish.set()
    await asyncio.gather(first, second)
    assert calls == ['123456']
    store.close()
