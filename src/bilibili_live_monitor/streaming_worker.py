"""千问有状态流式worker；每房间独立上下文，stdout仅JSONL。"""
import contextlib
import json
import os
import sys
import time
import uuid
import wave


class Streams:
    def __init__(self, model, engine, numpy):
        self.model, self.engine, self.np = model, engine, numpy
        self.streams = {}
        self.epoch = uuid.uuid4().hex

    def snapshot(self, slot, *, final=False, text=None, end=None):
        state = slot['state']
        committed = state.committed_text
        stable = state.stable_text[len(committed):].lstrip() if state.stable_text.startswith(committed) else ''
        return {'window_id': slot['window'], 'text': state.window_text if text is None else text,
                'stable_text': (state.window_text if text is None else text) if final else stable,
                'final': final, 'start_seconds': slot['start']/16000,
                'end_seconds': (slot['base']+slot['received']-len(state.buffer))/16000 if end is None else end/16000}

    def handle(self, request):
        sid = request['stream_id']
        if request['op'] == 'finish':
            slot = self.streams.pop(sid, None)
            if not slot:
                return {'updates': [], 'epoch': self.epoch}
            return {'updates': self.finish(slot), 'epoch': self.epoch}
        with wave.open(request['wav_path'], 'rb') as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
                raise ValueError('流式PCM必须为16kHz单声道int16')
            pcm = self.np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(self.np.float32)/32768
        offset = request['sample_offset']
        slot = self.streams.get(sid)
        updates = []
        if slot is None or slot['base']+slot['received'] != offset:
            # 丢包不能拼接成连续音频；提交旧上下文并从真实偏移重建。
            if slot:
                updates.extend(self.finish(slot))
            slot = {'state': self.engine.init_streaming(model=os.getenv('ASR_MODEL', 'Qwen/Qwen3-ASR-1.7B'),
                                                       chunk_size_sec=5, max_context_sec=30),
                    'base': offset, 'received': 0, 'start': offset,
                    'window': uuid.uuid4().hex}
            self.streams[sid] = slot
        for start in range(0, len(pcm), 80000):
            state = slot['state']
            before = state.committed_text
            part = pcm[start:start+80000]
            slot['received'] += len(part)
            slot['state'] = self.engine.feed_audio(part, state, model=self.model)
            processed = slot['base']+slot['received']-len(state.buffer)
            window_start = processed-len(state.audio_accum)
            if window_start > slot['start']:
                committed = state.committed_text[len(before):].lstrip()
                updates.append(self.snapshot(slot, final=True, text=committed, end=window_start))
                slot.update(start=window_start, window=uuid.uuid4().hex)
            updates.append(self.snapshot(slot))
        return {'updates': updates, 'epoch': self.epoch}

    def finish(self, slot):
        before = slot['state'].committed_text
        state = self.engine.finish_streaming(slot['state'], model=self.model)
        slot['state'] = state
        window_start = slot['base']+slot['received']-len(state.audio_accum)
        updates = []
        if window_start > slot['start']:
            updates.append(self.snapshot(slot, final=True,
                                         text=state.committed_text[len(before):].lstrip(), end=window_start))
            slot.update(start=window_start, window=uuid.uuid4().hex)
        updates.append(self.snapshot(slot, final=True))
        return updates


def main():
    with contextlib.redirect_stdout(sys.stderr):
        import numpy as np
        from mlx_qwen3_asr import load_model, streaming
        model, _ = load_model(os.getenv('ASR_MODEL', 'Qwen/Qwen3-ASR-1.7B'))
        streams = Streams(model, streaming, np)
    print(json.dumps({'ready': True}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        started = time.monotonic()
        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = streams.handle(request)
            result.update(req_id=request['req_id'], infer_ms=round((time.monotonic()-started)*1000), error=None)
        except Exception as error:
            result = {'req_id': request['req_id'], 'error': f'{type(error).__name__}: {error}'}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
