import asyncio
import sys

import numpy as np
from fastapi.testclient import TestClient

from bilibili_live_monitor.app import create_app
from bilibili_live_monitor.asr import ASR
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.load_metrics import LoadMetrics
from bilibili_live_monitor.monitor import Monitor
from bilibili_live_monitor.store import Store
from bilibili_live_monitor.vad import VoiceDetector


class FakeSession:
    def __init__(self, probability):
        self.probability = probability
        self.states = []

    def run(self, _, inputs):
        assert inputs['input'].shape == (1, 576)
        assert inputs['state'].shape == (2, 1, 128)
        assert inputs['sr'].dtype == np.int64
        self.states.append(inputs['state'].copy())
        return np.array([[self.probability]]), inputs['state'] + 1


def test_thirty_second_tail_is_carried_without_zero_state_and_reset_prevents_revival(tmp_path):
    class TailSession(FakeSession):
        def __init__(self):
            super().__init__(0.6)
            self.inputs = []

        def run(self, names, inputs):
            self.inputs.append(inputs['input'].copy())
            return super().run(names, inputs)

    session = TailSession()
    vad = VoiceDetector(Settings(data_dir=tmp_path), session)
    pcm = (np.ones(480000, dtype='<i2') * 8192).tobytes()
    token = vad.prepare('a')
    assert vad.analyze(pcm, 'a', token)['vad_has_speech']
    assert len(token['tail']) == 256
    assert token['state'][0, 0, 0] == 937  # 第938次仅预览，不提交补零状态。
    assert np.all(token['context'] == 0.25)
    vad.analyze(bytes(512), 'a', token)
    assert np.all(session.inputs[938][0, :64] == 0.25)
    assert np.all(session.inputs[938][0, 64:320] == 0.25)
    vad.reset('a')
    vad.analyze(pcm, 'a', token)
    assert 'a' not in vad.states


def test_state_api_accepts_new_statuses_and_keeps_load_contract(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    store = Store(tmp_path / 'monitor.sqlite3')
    store.room({'room_id': '123456', 'url': 'https://live.bilibili.com/123456', 'title': 'synthetic',
                'cover_url': '', 'chunk_seconds': 30, 'status': 'stopped', 'error': None,
                'muted': True, 'cycle_notice': False})
    for seq, status in enumerate(('no_speech', 'dropped'), 1):
        store.segment({'room_id': '123456', 'seq': seq, 'status': status, 'text': '', 'error': None,
                       'start_utc': '2026-01-01T00:00:00+00:00', 'end_utc': '2026-01-01T00:00:30+00:00',
                       'duration_seconds': 30, 'muted': True, 'paused': False, 'track_live': True,
                       'rms_dbfs': -30, 'queue_wait_ms': 0, 'infer_ms': 0})
    store.close()

    async def no_start(self):
        pass

    monkeypatch.setattr(Monitor, 'start', no_start)
    with TestClient(create_app(settings)) as client:
        response = client.get('/api/state')
        assert response.status_code == 200
        state = response.json()
        assert [segment['status'] for segment in state['rooms'][0]['segments']] == ['no_speech', 'dropped']
        assert 'asr_busy_ratio' in state and 'vad_error' in state
        assert 'queue_audio_seconds' in state and 'no_speech_skip_ratio' in state


def test_room_vad_state_isolated_and_no_audio_trim(tmp_path):
    session = FakeSession(0.7)
    vad = VoiceDetector(Settings(data_dir=tmp_path), session)
    pcm = np.ones(16000, dtype='<i2').tobytes()
    assert vad.analyze(pcm, 'a')['vad_has_speech']
    assert vad.analyze(pcm, 'a')['vad_speech_seconds'] == 1
    assert session.states[32][0, 0, 0] == 31
    vad.analyze(pcm, 'b')
    assert session.states[64][0, 0, 0] == 0
    vad.reset('a')
    assert 'a' not in vad.states
    assert len(pcm) == 32000


async def test_vad_skips_music_but_keeps_timestamp_and_fails_open(tmp_path):
    store = Store(tmp_path / 'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    monitor.generations['123456'] = 'a'
    monitor.vad = VoiceDetector(monitor.settings, FakeSession(0.1))
    pcm = (np.ones(16000, dtype='<i2') * 1000).tobytes()
    base = {'room_id': '123456', 'rms_dbfs': -30, 'start_utc': '2026-01-01T00:00:00+00:00',
            'end_utc': '2026-01-01T00:00:01+00:00'}
    await monitor.classify_and_submit({**base, 'seq': 1}, pcm, 'a')
    segment = store.segments('123456')[0]
    assert segment['status'] == 'no_speech'
    assert segment['end_utc'] == base['end_utc']
    assert monitor.queue.empty()

    def broken(*args):
        raise RuntimeError('synthetic failure')

    monitor.vad.analyze = broken
    await monitor.classify_and_submit({**base, 'seq': 2}, pcm, 'a')
    assert monitor.queue.qsize() == 1
    assert monitor.vad_error_count == 1
    assert store.segments('123456')[-1]['vad_error']
    monitor.cancel_queued('123456')
    await asyncio.wait_for(monitor.queue.join(), 1)
    store.close()


def test_load_metrics_clips_busy_intervals_at_window_boundary(monkeypatch):
    import bilibili_live_monitor.load_metrics as module

    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    load = LoadMetrics(window=10)
    load.begin()
    clock[0] = 108
    load.end()
    load.windows.extend([(101, 1, None), (109, 1, 'no_speech')])
    clock[0] = 112
    values = load.snapshot()
    assert values['asr_busy_ratio'] == 0.6
    assert values['vad_skip_ratio'] == 1
    load.begin()
    clock[0] = 114
    assert load.snapshot()['asr_busy_ratio'] == 0.6


async def test_oldest_eviction_balances_join_and_preserves_other_rooms(tmp_path):
    store = Store(tmp_path / 'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path, queue_size=2), store)
    pcm = bytes(32000)
    for room, seq in [('123456', 1), ('234567', 1), ('123456', 2)]:
        monitor.submit({'room_id': room, 'seq': seq, 'rms_dbfs': -20,
                        'duration_seconds': 1}, pcm, room)
    assert store.segments('123456')[0]['status'] == 'dropped'
    assert [(row[0]['room_id'], row[0]['seq']) for row in monitor.queue.snapshot()] == [
        ('234567', 1), ('123456', 2)]
    assert monitor.metrics()['queue_audio_seconds'] == 2
    monitor.cancel_queued('123456')
    monitor.cancel_queued('234567')
    await asyncio.wait_for(monitor.queue.join(), 1)
    assert not list((tmp_path / 'audio').iterdir())
    store.close()


async def test_vad_late_result_after_stop_cannot_enqueue(tmp_path):
    store = Store(tmp_path / 'test.db')
    monitor = Monitor(Settings(data_dir=tmp_path), store)
    monitor.generations['123456'] = 'a'

    def stopped(*args):
        monitor.generations.pop('123456')
        return {'vad_has_speech': True}

    monitor.vad.analyze = stopped
    await monitor.classify_and_submit({'room_id': '123456', 'seq': 1, 'rms_dbfs': -20}, bytes(32000), 'a')
    assert monitor.queue.empty()
    assert not store.segments('123456')
    store.close()


async def test_queue_wait_longer_than_inference_timeout_still_completes(tmp_path):
    worker = tmp_path / 'worker.py'
    worker.write_text('''import json,sys,time
print(json.dumps({'ready':True}),flush=True)
for line in sys.stdin:
 r=json.loads(line);time.sleep(0.1)
 print(json.dumps({'req_id':r['req_id'],'text':'synthetic','infer_ms':100}),flush=True)
''')
    settings = Settings(data_dir=tmp_path, asr_python=sys.executable, asr_infer_timeout=0.25)
    store = Store(tmp_path / 'test.db')
    monitor = Monitor(settings, store)
    monitor.asr = ASR(settings, worker_script=worker)
    monitor.generations['123456'] = 'a'
    await monitor.start(warm=False)
    try:
        for seq in range(1, 5):
            monitor.submit({'room_id': '123456', 'seq': seq, 'rms_dbfs': -20}, bytes(32000), 'a')
        await asyncio.wait_for(monitor.queue.join(), 3)
        segments = store.segments('123456')
        assert all(segment['status'] == 'done' for segment in segments)
        assert segments[-1]['queue_wait_ms'] > 250
        assert monitor.asr.timeout_count == 0
        assert monitor.asr.restart_count == 0
    finally:
        await monitor.close()
        store.close()
