"""Silero ONNX 窗口级语音门控，不裁剪音频或改写时间戳。"""
import threading
import time
from pathlib import Path

import numpy as np


class VoiceDetector:
    def __init__(self, settings, session=None):
        self.settings = settings
        self.session = session
        self.states = {}
        self.lock = threading.Lock()

    def _load(self):
        with self.lock:
            if self.session is None:
                import onnxruntime as ort

                path = Path(self.settings.vad_model_path).expanduser()
                if not path.is_file():
                    raise FileNotFoundError('缺少VAD模型，请设置VAD_MODEL_PATH；未执行人声过滤')
                options = ort.SessionOptions()
                options.inter_op_num_threads = 1
                options.intra_op_num_threads = 1
                self.session = ort.InferenceSession(str(path), sess_options=options,
                                                   providers=['CPUExecutionProvider'])

    def prepare(self, key):
        with self.lock:
            return self.states.setdefault(key, {
                'state': np.zeros((2, 1, 128), dtype=np.float32),
                'context': np.zeros(64, dtype=np.float32),
                'tail': np.empty(0, dtype=np.float32), 'lock': threading.Lock()})

    def reset(self, key):
        with self.lock:
            self.states.pop(key, None)

    def analyze(self, pcm, key, token=None):
        started = time.monotonic()
        token = token if token is not None else self.prepare(key)
        self._load()
        # ONNX session可并发调用；同房间串行，房间之间不持整窗全局锁。
        with token['lock']:
            state, context = token['state'], token['context']
            samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
            samples = np.concatenate((token['tail'], samples))
            voiced = 0
            peak = 0.0
            boundary_peak = 0.0
            full_end = len(samples) // 512 * 512
            for offset in range(0, full_end, 512):
                frame = samples[offset:offset + 512]
                audio = np.concatenate((context, frame))[None, :]
                output, state = self.session.run(None, {
                    'input': audio, 'state': state, 'sr': np.array(16000, dtype=np.int64)})
                probability = float(np.asarray(output).reshape(-1)[0])
                peak = max(peak, probability)
                if offset == 0 or offset + 512 == full_end:
                    boundary_peak = max(boundary_peak, probability)
                if probability >= self.settings.vad_threshold:
                    voiced += 512
                context = frame[-64:].copy()
            tail = samples[full_end:].copy()
            if len(tail):
                # 尾帧只做临时预览以保留边界人声，补零结果不写回循环状态。
                preview = np.concatenate((context, np.pad(tail, (0, 512 - len(tail)))))[None, :]
                output, _ = self.session.run(None, {
                    'input': preview, 'state': state.copy(), 'sr': np.array(16000, dtype=np.int64)})
                probability = float(np.asarray(output).reshape(-1)[0])
                peak = max(peak, probability)
                boundary_peak = max(boundary_peak, probability)
                if probability >= self.settings.vad_threshold:
                    voiced += len(tail)
            with self.lock:
                # reset移除token后，已取消线程不能复活旧generation。
                if self.states.get(key) is token:
                    token.update(state=state, context=context, tail=tail)
        speech_seconds = min(len(pcm) / 32000, voiced / 16000)
        has_speech = (speech_seconds >= self.settings.vad_min_speech_seconds or peak >= 0.9 or
                      boundary_peak >= self.settings.vad_threshold)
        return {'vad_speech_seconds': round(speech_seconds, 3), 'vad_peak': round(peak, 4),
                'vad_ms': round((time.monotonic() - started) * 1000), 'vad_has_speech': has_speech}
