"""将网页实际播放的16kHz单声道PCM分段保存，不另请求媒体流。"""
import time
import uuid
import wave
from datetime import datetime, timezone


class PCMRecording:
    def __init__(self, settings, room):
        self.room = room
        self.directory = settings.data_dir / 'recordings' / room['room_id']
        self.directory.mkdir(parents=True, exist_ok=True)
        self.session = room.setdefault('recording_session', uuid.uuid4().hex[:8])
        self.prefix = self.session + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        self.limit = max(1, int(settings.recording_segment_seconds)) * 32000
        self.total_bytes = self.file_bytes = self.index = 0
        self.base_seconds = room.get('recorded_seconds', 0)
        self.progress_seconds = 0
        self.last_progress = time.monotonic()
        self.writer = self.file = None
        self.closed = False

    def _close_file(self):
        if self.writer:
            try:
                self.writer.close()
            finally:
                try:
                    self.file.close()
                finally:
                    self.writer = self.file = None

    def write(self, pcm):
        if self.closed or len(pcm) % 2:
            raise ValueError('PCM录音已关闭或样本未对齐')
        while pcm:
            if self.writer is None:
                self.file = (self.directory / f'{self.prefix}-{self.index:05d}.wav').open('xb')
                self.writer = wave.open(self.file, 'wb')
                self.writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                self.file_bytes = 0
                self.index += 1
            size = min(len(pcm), self.limit - self.file_bytes)
            self.writer.writeframes(pcm[:size])
            self.file.flush()
            self.file_bytes += size
            self.total_bytes += size
            pcm = pcm[size:]
            self.progress_seconds = self.total_bytes / 32000
            self.last_progress = time.monotonic()
            if self.file_bytes == self.limit:
                self._close_file()

    def snapshot(self):
        files = [p for p in self.directory.glob(f'{self.session}-*')
                 if p.suffix in ('.wav', '.mkv', '.mka') and p.is_file()]
        return {'recording_status': 'recording' if self.total_bytes else 'starting',
                'recorded_seconds': round(self.base_seconds + self.progress_seconds, 3),
                'recording_files': len(files), 'recording_bytes': sum(p.stat().st_size for p in files)}

    async def check(self):
        if time.monotonic() - self.last_progress > 40:
            raise RuntimeError('recording_stalled：40秒没有新音频进度')

    async def close(self):
        try:
            self._close_file()
        finally:
            self.closed = True
