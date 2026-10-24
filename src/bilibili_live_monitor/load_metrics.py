"""有界、基于单调时钟的近期负载统计。"""
import time
from collections import deque


class LoadMetrics:
    def __init__(self, window=300):
        self.window = window
        self.started = time.monotonic()
        self.busy_since = None
        self.busy = deque(maxlen=10000)
        self.windows = deque(maxlen=10000)
        self.results = deque(maxlen=10000)

    def begin(self):
        self.busy_since = time.monotonic()

    def end(self):
        now = time.monotonic()
        if self.busy_since is not None:
            self.busy.append((self.busy_since, now))
            self.busy_since = None

    def snapshot(self):
        now = time.monotonic()
        cutoff = max(self.started, now - self.window)
        while self.busy and self.busy[0][1] < cutoff:
            self.busy.popleft()
        for values in (self.windows, self.results):
            while values and values[0][0] < cutoff:
                values.popleft()
        busy = sum(end - max(start, cutoff) for start, end in self.busy)
        if self.busy_since is not None:
            busy += now - max(cutoff, self.busy_since)
        elapsed = now - cutoff
        duration = sum(seconds for _, seconds, _ in self.windows)
        def ratio(kind=None):
            seconds = sum(seconds for _, seconds, reason in self.windows
                          if reason is not None and (kind is None or reason == kind))
            return round(seconds / duration, 4) if duration else None
        return {'load_window_seconds': round(elapsed, 1),
                'asr_busy_ratio': round(busy / elapsed, 4) if elapsed else 0,
                'vad_skip_ratio': ratio(), 'silence_skip_ratio': ratio('silent'),
                'no_speech_skip_ratio': ratio('no_speech'),
                'valid_windows_recent': len(self.windows),
                'completed_recent': len(self.results),
                'mean_result_latency_ms': (round(sum(value for _, value in self.results) / len(self.results))
                                           if self.results else None)}
