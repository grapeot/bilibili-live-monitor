"""原始压缩包分段录制；音频模式不解码视频、不重编码音频。"""
import asyncio
import re
import signal
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit


def is_media_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    return (parsed.scheme in ('http', 'https') and
            any(host.endswith('.' + domain) for domain in ('bilivideo.com', 'bilivideo.cn', 'bilivideo.net')) and
            parsed.path.endswith(('.flv', '.m3u8')))


def command(url, room_url, mode, destination, seconds):
    maps = ['-map', '0:a:0', '-vn'] if mode == 'audio' else ['-map', '0:v:0', '-map', '0:a:0']
    network = (['-rw_timeout', '15000000', '-headers', f'Referer: {room_url}\r\n']
               if urlsplit(url).scheme in ('http', 'https') else [])
    return ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning', *network, '-i', url, *maps, '-c', 'copy',
            '-f', 'segment', '-segment_time', str(seconds), '-reset_timestamps', '1',
            '-segment_format', 'matroska', '-progress', 'pipe:1', str(destination)]


class Recording:
    def __init__(self, settings, room):
        self.settings, self.room = settings, room
        self.process = None
        self.tasks = []
        self.error_lines = []
        self.progress_seconds = 0
        self.base_seconds = room.get('recorded_seconds', 0)
        self.last_progress = time.monotonic()
        self.closed = False
        self.close_task = None
        self.refused = None
        self.directory = settings.data_dir / 'recordings' / room['room_id']
        self.session = room.setdefault('recording_session', uuid.uuid4().hex[:8])
        self.prefix = self.session + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]

    async def start(self, url):
        self.directory.mkdir(parents=True, exist_ok=True)
        extension = 'mka' if self.room['record_mode'] == 'audio' else 'mkv'
        destination = self.directory / f'{self.prefix}-%05d.{extension}'
        self.process = await asyncio.create_subprocess_exec(
            *command(url, self.room['url'], self.room['record_mode'], destination,
                     self.settings.recording_segment_seconds),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.tasks = [asyncio.create_task(self.read_progress()), asyncio.create_task(self.read_errors())]

    async def read_progress(self):
        while line := await self.process.stdout.readline():
            key, _, value = line.decode(errors='replace').strip().partition('=')
            if key == 'out_time_us':
                try:
                    seconds = max(0, int(value) / 1_000_000)
                    if seconds > self.progress_seconds:
                        self.last_progress = time.monotonic()
                    self.progress_seconds = seconds
                except ValueError:
                    pass

    async def read_errors(self):
        while line := await self.process.stderr.readline():
            text = re.sub(r'https?://\S+', '<media-url>', line.decode(errors='replace').strip())
            refusal = re.search(r'(?:HTTP error|Server returned)\s+(403|412|429)\b', text, re.I)
            if refusal:
                self.refused = refusal[1]
            self.error_lines = (self.error_lines + [text])[-12:]

    def snapshot(self):
        files = list(self.directory.glob(f'{self.session}-*.mk*'))
        return {'recording_status': 'recording' if self.progress_seconds > 0 else 'starting',
                'recorded_seconds': round(self.base_seconds + self.progress_seconds, 3),
                'recording_files': len(files), 'recording_bytes': sum(p.stat().st_size for p in files)}

    async def check(self):
        if self.refused:
            raise RuntimeError('recording_refused: HTTP ' + self.refused)
        if self.process.returncode is not None:
            await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.refused:
                raise RuntimeError('recording_refused: HTTP ' + self.refused)
            # 不把鉴权或限流拒绝当成普通断线反复请求。
            refused = any(re.search(r'\b(403|412|429)\b', line) for line in self.error_lines)
            reason = 'recording_refused' if refused else 'recording_stream_ended'
            raise RuntimeError(reason + ': ' + ' | '.join(self.error_lines)[-900:])
        if time.monotonic() - self.last_progress > 40:
            raise RuntimeError('recording_stalled：40秒没有新媒体进度')

    async def close(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.closed = True
