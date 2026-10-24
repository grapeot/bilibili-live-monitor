import asyncio
import json
import subprocess
import sys
import time

import numpy as np
import pytest

from bilibili_live_monitor.asr import ASR
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.monitor import Monitor, rms_dbfs, validate_source
from bilibili_live_monitor.runtime import parse_deadline, run_owned
from bilibili_live_monitor.store import Store


async def test_stop_releases_queue_and_inflight_without_dropping_other_room(tmp_path):
    store = Store(tmp_path / 'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path, queue_size=1, asr_enabled=True, record_mode='none'), store)
    store.room({'room_id': '123456', 'status': 'capturing'})
    monitor.generations.update({'123456': 'a', '234567': 'b'})
    started = asyncio.Event()

    async def recognize(path, req_id):
        if req_id.startswith('a:'):
            started.set()
            await asyncio.Future()
        return {'text': '合成B结果', 'infer_ms': 1}

    monitor.asr.transcribe = recognize
    await monitor.start(warm=False)
    pcm = (np.ones(16000, dtype='<i2') * 1000).tobytes()

    def submit(room_id, seq, generation):
        monitor.submit({'room_id': room_id, 'seq': seq, 'rms_dbfs': rms_dbfs(pcm),
                        'text': '', 'error': None}, pcm, generation)

    try:
        submit('123456', 1, 'a')
        await asyncio.wait_for(started.wait(), 1)
        submit('123456', 2, 'a')
        assert monitor.queue.full()
        await monitor.stop('123456')
        assert not monitor.queue.full()
        submit('234567', 1, 'b')
        await asyncio.wait_for(monitor.queue.join(), 2)
        assert monitor.overload_count == 0
        assert [s['status'] for s in store.segments('123456')] == ['cancelled', 'cancelled']
        assert store.segments('234567')[0]['text'] == '合成B结果'
        assert not list((tmp_path / 'audio').iterdir())
    finally:
        await monitor.close()
        store.close()


async def test_interleaved_success_resets_retry_budget(monkeypatch, tmp_path):
    from bilibili_live_monitor import monitor as module
    store = Store(tmp_path / 'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path, max_reconnects=2, retry_delay=0), store)
    room = {'room_id': '123456', 'status': 'capturing', 'chunk_seconds': 30, 'error': None}
    monitor.generations['123456'] = 'session'
    calls = 0

    class Page:
        reloads = 0
        closed = False

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

    async def noop(*args):
        pass

    async def read(*args):
        nonlocal calls
        calls += 1
        if calls in (1, 3, 5):
            raise RuntimeError('合成瞬断')
        if calls == 6:
            raise asyncio.CancelledError()
        return bytes(32000*30), {'started': 1000, 'ended':31000, 'muted':True, 'paused':False,
            'track_live':True, 'context_state':'running', 'missing_samples':0, 'dropped_samples':0,
            'media_advanced':30, 'pauses':0, 'play_error':None, 'track_muted':False,
            'track_enabled':True, 'track_muted_ms':0}

    monkeypatch.setattr(module, 'start_pcm', noop)
    monkeypatch.setattr(module, 'close_pcm', noop)
    monkeypatch.setattr(module, 'read_pcm', read)
    page = Page()
    with pytest.raises(asyncio.CancelledError):
        await monitor.capture(room, page, 'session')
    assert page.reloads == 3  # 总失败3次，但从未连续失败2次。
    assert len(store.segments('123456')) == 2
    assert all(s['status'] == 'silent' for s in store.segments('123456'))
    assert page.closed
    store.close()


async def test_close_failure_still_releases_room_slot(tmp_path):
    store = Store(tmp_path / 'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)

    class Page:
        def is_closed(self):
            return False

        async def close(self):
            raise RuntimeError('合成关闭失败')

    room = {'room_id':'123456'}
    monitor.tasks['123456'] = asyncio.current_task()
    monitor.pages['123456'] = Page()
    await monitor.capture(room, monitor.pages['123456'], '已失效的会话')
    assert '123456' not in monitor.tasks and '123456' not in monitor.pages
    assert store.events(0)[0]['kind'] == 'cleanup_error'
    store.close()


async def test_asr_crash_retries_same_segment_once(tmp_path):
    worker = tmp_path / 'worker.py'
    worker.write_text('''import sys,json,pathlib
p=pathlib.Path(__file__).with_suffix('.seen')
print(json.dumps({'ready':True}),flush=True)
for line in sys.stdin:
 r=json.loads(line)
 if not p.exists():
  p.touch();sys.exit(3)
 print(json.dumps({'req_id':r['req_id'],'text':'合成恢复结果','infer_ms':1}),flush=True)
''')
    settings = Settings(data_dir=tmp_path, asr_python=sys.executable)
    asr = ASR(settings, worker_script=worker)
    try:
        result = await asr.transcribe(tmp_path / 'synthetic.wav', 'request-1')
        assert result['text'] == '合成恢复结果'
        assert asr.restart_count == 1 and asr.ready
    finally:
        await asr.close()
    assert not asr.ready


async def test_orphan_audio_cleanup_is_scoped(tmp_path):
    directory = tmp_path / 'audio'
    directory.mkdir()
    orphan = directory / ('a'*32 + '.wav')
    orphan.write_bytes(b'owned')
    unrelated = directory / 'example.wav'
    unrelated.write_bytes(b'not-owned')
    store = Store(tmp_path / 'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    await monitor.start(warm=False)
    assert not orphan.exists() and unrelated.exists()
    await monitor.close()
    store.close()


def test_deadline_kills_owned_process_and_descendant(tmp_path):
    code = '''import signal,subprocess,sys,time,pathlib
signal.signal(signal.SIGTERM,signal.SIG_IGN)
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
pathlib.Path(sys.argv[1]).write_text(str(p.pid))
time.sleep(60)
'''
    pidfile = tmp_path / 'descendant.pid'
    result = run_owned([('synthetic', [sys.executable, '-c', code, str(pidfile)])],
                       tmp_path, time.time()+1.5)
    assert result['status'] == 'stopped' and result['reason'] == 'deadline'
    child_pid = int(pidfile.read_text())
    status = subprocess.run(['ps','-p',str(child_pid),'-o','stat='], capture_output=True, text=True)
    assert not status.stdout.strip() or status.stdout.strip().startswith('Z')
    assert json.loads((tmp_path / 'runtime.json').read_text())['status'] == 'stopped'


def test_past_or_naive_deadline_is_rejected():
    for value in ('2000-01-01T22:00:00-08:00', '2030-01-01T22:00:00'):
        with pytest.raises(ValueError):
            parse_deadline(value)


async def test_worker_error_response_retries_without_reloading_model(tmp_path):
    worker = tmp_path / 'worker.py'
    worker.write_text('''import sys,json
print(json.dumps({'ready':True}),flush=True)
attempt=0
for line in sys.stdin:
 r=json.loads(line);attempt+=1
 result={'req_id':r['req_id'],'error':'合成识别错误'} if attempt==1 else {'req_id':r['req_id'],'text':'合成恢复结果','infer_ms':1}
 print(json.dumps(result),flush=True)
''')
    asr = ASR(Settings(data_dir=tmp_path, asr_python=sys.executable), worker_script=worker)
    try:
        await asr.start()
        pid = asr.process.pid
        result = await asr.transcribe(tmp_path / 'synthetic.wav', 'same-request')
        assert result['text'] == '合成恢复结果'
        assert asr.process.pid == pid
        assert asr.retry_count == 1 and asr.restart_count == 0
    finally:
        await asr.close()


async def test_completed_inference_cannot_publish_after_stop(tmp_path):
    store = Store(tmp_path / 'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path, asr_enabled=True, record_mode='none'), store)
    monitor.generations['123456'] = 'a'
    store.room({'room_id':'123456', 'status':'capturing'})
    stop_tasks = []

    async def recognize(*args):
        # 完成结果前调度stop，令stop在consume恢复前运行；cancel已完成Task不会生效。
        stop_tasks.append(asyncio.create_task(monitor.stop('123456')))
        return {'text':'不应发布的合成结果', 'infer_ms':1}

    monitor.asr.transcribe = recognize
    await monitor.start(warm=False)
    pcm = (np.ones(16000, dtype='<i2')*1000).tobytes()
    monitor.submit({'room_id':'123456','seq':1,'rms_dbfs':rms_dbfs(pcm),'text':''}, pcm, 'a')
    await asyncio.wait_for(monitor.queue.join(), 2)
    await asyncio.gather(*stop_tasks)
    segment = store.segments('123456')[0]
    assert segment['status'] == 'cancelled' and segment['text'] == ''
    assert all(json.loads(e['data']).get('status') != 'done' for e in store.events(0))
    await monitor.close()
    store.close()


def test_live_but_muted_or_disabled_input_is_not_valid_silence():
    normal = {'muted':True,'paused':False,'track_live':True,'context_state':'running',
              'track_enabled':True,'track_muted':False,'track_muted_ms':0,'play_error':None}
    validate_source(normal)
    for patch in ({'track_muted':True}, {'track_enabled':False}, {'track_muted_ms':101}):
        with pytest.raises(RuntimeError, match='媒体状态异常'):
            validate_source({**normal, **patch})
