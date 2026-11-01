import sys
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from bilibili_live_monitor.asr import StreamingASR
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.monitor import Monitor
from bilibili_live_monitor.store import Store
from bilibili_live_monitor.streaming_worker import Streams


class FakeEngine:
    """替代声学推理，测生产封装的房间隔离、窗口换页和缺口处理。"""
    @staticmethod
    def init_streaming(**kwargs):
        return SimpleNamespace(buffer=np.array([]), audio_accum=np.array([]),
                               committed_text='', window_text='', stable_text='')

    @staticmethod
    def feed_audio(pcm, state, **kwargs):
        if len(state.audio_accum)+len(pcm) > 480000:
            state.committed_text += state.window_text
            state.audio_accum = np.array([])
        state.audio_accum = np.concatenate([state.audio_accum, pcm])
        state.window_text = str(round(float(state.audio_accum.mean()), 2))
        state.stable_text = state.committed_text + state.window_text
        return state

    @staticmethod
    def finish_streaming(state, **kwargs):
        return state


def request(tmp_path, sid, offset=0, value=1000):
    path = tmp_path / f'{sid}.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(np.full(80000, value, dtype='<i2').tobytes())
    return {'op': 'feed', 'stream_id': sid, 'sample_offset': offset, 'wav_path': str(path)}


def test_worker_rooms_keep_independent_context_and_finish_releases_state(tmp_path):
    worker = Streams(None, FakeEngine, np)
    a = worker.handle(request(tmp_path, 'a', value=1000))['updates'][0]
    b = worker.handle(request(tmp_path, 'b', value=12000))['updates'][0]
    assert a['text'] != b['text']
    again = worker.handle(request(tmp_path, 'a', offset=80000))['updates'][0]
    assert a['window_id'] == again['window_id'] and again['end_seconds'] == 10
    final = worker.handle({'op': 'finish', 'stream_id': 'a'})['updates'][0]
    assert final['final'] and final['window_id'] == a['window_id']
    assert set(worker.streams) == {'b'}


def test_worker_commits_window_and_does_not_join_across_dropped_audio(tmp_path):
    worker = Streams(None, FakeEngine, np)
    for i in range(6):
        result = worker.handle(request(tmp_path, 'a', offset=80000*i))
    before = result['updates'][-1]
    assert before['end_seconds'] == 30
    updates = worker.handle(request(tmp_path, 'a', offset=480000))['updates']
    assert len(updates) == 2 and updates[0]['final']
    assert updates[0]['window_id'] == before['window_id']
    assert updates[1]['window_id'] != before['window_id'] and updates[1]['start_seconds'] == 30
    gap = worker.handle(request(tmp_path, 'a', offset=640000))['updates']
    assert gap[0]['final'] and gap[-1]['start_seconds'] == 40
    assert gap[-1]['end_seconds'] == 45


def test_publisher_revisions_update_same_row_and_restart_keeps_history(tmp_path):
    store = Store(tmp_path / 'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    segment = {'room_id': '123456', 'stream_id': 'stream', 'origin_ms': 1000}
    def publish(text, final=False, epoch='one'):
        monitor.publish_stream(segment, {'epoch': epoch, 'updates': [
            {'window_id': 'window', 'text': text, 'stable_text': '',
             'start_seconds': 0, 'end_seconds': 5 if not final else 10, 'final': final}]})
    publish('草稿')
    first = store.items('123456')[0]
    publish('修订稿')
    assert len(store.items('123456')) == 1
    assert store.items('123456')[0]['id'] == first['id']
    publish('定稿', final=True)
    assert store.items('123456')[0]['text'] == '定稿'
    assert store.items('123456')[0]['is_final']
    assert store.db.execute('SELECT at_utc FROM timeline').fetchone()[0] == store.items('123456')[0]['at_utc']
    publish('重启前')
    publish('新上下文', epoch='two')
    rows = sorted(store.items('123456'), key=lambda row: row['seq'])
    assert len(rows) == 3 and rows[-2]['status'] == 'cancelled'
    assert rows[-1]['transcript_version'] == 'realtime'
    store.close()


@pytest.mark.asyncio
async def test_stream_error_restarts_before_replaying_audio(tmp_path):
    worker = tmp_path / 'fake_worker.py'
    worker.write_text('''import sys,json,pathlib,os
marker=pathlib.Path(__file__).with_suffix('.seen')
print(json.dumps({'ready':True}),flush=True)
for line in sys.stdin:
 r=json.loads(line)
 if not marker.exists():
  marker.touch(); print(json.dumps({'req_id':r['req_id'],'error':'已消费后失败'}),flush=True)
 else:
  print(json.dumps({'req_id':r['req_id'],'epoch':str(os.getpid()),'updates':[]}),flush=True)
''')
    asr = StreamingASR(Settings(data_dir=tmp_path, asr_python=sys.executable), worker)
    try:
        await asr.start()
        pid = asr.process.pid
        result = await asr.feed(tmp_path/'not_read.wav', 'one', 'stream', 0)
        assert result['epoch'] != str(pid)
        assert asr.retry_count == asr.restart_count == 1
    finally:
        await asr.close()
