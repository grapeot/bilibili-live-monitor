import asyncio
import math
import time
import uuid
import wave
from datetime import datetime, timezone

import numpy as np

from .asr import ASR
from .audio_queue import AudioQueue
from .browser import Browser, close_page, ensure_playing, room_url
from .capture import close_pcm, read_pcm, start_pcm


def iso_ms(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()


def rms_dbfs(pcm):
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
    if not len(samples):
        return -120.0
    return round(max(-120.0, 20 * math.log10(float(np.sqrt(np.mean(samples ** 2))) + 1e-12)), 1)


def window_metadata(pcm, start_ms, end_ms):
    duration = len(pcm) / 32000
    elapsed = (end_ms - start_ms) / 1000
    coverage = duration / elapsed if elapsed > 0 else 0
    if duration < 0.5 or not 0.90 <= coverage <= 1.20:
        raise RuntimeError(f'音频覆盖异常：墙钟{elapsed:.2f}秒，音频{duration:.2f}秒，'
                           f'覆盖率{coverage:.1%}；本段不计作完整采集')
    return {'start_utc': iso_ms(start_ms), 'end_utc': iso_ms(end_ms),
            'duration_seconds': duration, 'wall_seconds': round(elapsed, 3),
            'coverage_ratio': round(coverage, 4)}


def validate_source(payload):
    if (not payload['muted'] or payload['paused'] or not payload['track_live']
            or payload['context_state'] != 'running' or not payload['track_enabled']
            or payload['track_muted'] or payload['track_muted_ms'] > 100):
        raise RuntimeError(f'媒体状态异常：muted={payload["muted"]}, paused={payload["paused"]}, '
                           f'track_live={payload["track_live"]}, enabled={payload["track_enabled"]}, '
                           f'track_muted={payload["track_muted"]}, track_muted_ms={payload["track_muted_ms"]}, '
                           f'context={payload["context_state"]}, play_error={payload["play_error"]}')


class Monitor:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.browser = Browser(settings.cdp_url)
        self.asr = ASR(settings)
        self.queue = AudioQueue(maxsize=settings.queue_size)
        self.tasks, self.pages, self.generations = {}, {}, {}
        self.pending = set()
        self.stopping = set()
        self.starting_rooms = {}
        self.lock = asyncio.Lock()
        self.worker = None
        self.overload_count = 0
        self.inflight = None
        self.inference_task = None
        self.active_generation = None
        self.failures = {}
        self.warm_task = None

    async def start(self, *, warm=True):
        directory = self.settings.data_dir / 'audio'
        if directory.exists() and self.queue.empty():
            for path in directory.glob('*.wav'):
                if len(path.stem) == 32 and all(c in '0123456789abcdef' for c in path.stem):
                    path.unlink(missing_ok=True)
        if warm:
            self.warm_task = asyncio.create_task(self.warm_model())
        self.worker = asyncio.create_task(self.consume())

    async def warm_model(self):
        try:
            await self.asr.start()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.store.event('asr_error', {'error': f'{type(error).__name__}: {error}'})

    def state(self):
        rooms = self.store.rooms()
        for room in rooms:
            room['segments'] = self.store.segments(room['room_id'])
            room['items'] = self.store.items(room['room_id'])
        return {'rooms': rooms, **self.metrics(), 'cursor': self.store.cursor()}

    def metrics(self):
        return {'queue_depth': self.queue.qsize(), 'queue_capacity': self.queue.maxsize,
                'asr_ready': self.asr.ready, 'overload_count': self.overload_count,
                'inflight': self.inflight,
                'active_count': len(self.tasks), 'pending_count': len(self.pending),
                'asr_restarts': self.asr.restart_count, 'asr_retries': self.asr.retry_count,
                'asr_error': self.asr.last_error}

    async def add(self, value, seconds):
        url = room_url(value)
        async with self.lock:
            if url in self.pending or url.rsplit('/', 1)[1] in self.tasks or url.rsplit('/', 1)[1] in self.stopping:
                raise ValueError('该房间已在监控或启动中')
            if len(self.tasks) + len(self.pending) >= self.settings.max_rooms:
                raise ValueError('已达到同时监控上限')
            self.pending.add(url)
        page = None
        chat_context, chat_buffer = {}, []
        chat_dropped = 0

        def on_danmaku(message):
            nonlocal chat_dropped
            if not chat_context:
                if len(chat_buffer) < 200:
                    chat_buffer.append(message)
                else:
                    chat_dropped += 1
            elif self.generations.get(chat_context['room_id']) == chat_context['generation']:
                self.store.danmaku(chat_context['room_id'], message)

        async def on_resolved(resolved_page):
            room_id = room_url(resolved_page.url).rsplit('/', 1)[1]
            async with self.lock:
                if room_id in self.tasks or room_id in self.stopping or room_id in self.starting_rooms:
                    raise ValueError('同一实际房间已在监控或启动中')
                generation = uuid.uuid4().hex
                self.starting_rooms[room_id] = generation
                self.generations[room_id] = generation
                chat_context.update(room_id=room_id, generation=generation)
                self.store.room({'room_id': room_id, 'url': room_url(resolved_page.url),
                                 'title': room_id, 'cover_url': '',
                                 'chunk_seconds': seconds, 'status': 'reconnecting',
                                 'error': None, 'muted': True, 'cycle_notice': False})
                for message in chat_buffer:
                    on_danmaku(message)
                chat_buffer.clear()
                if chat_dropped:
                    self.store.item({'room_id': room_id, 'kind': 'danmaku',
                                     'at_utc': iso_ms(time.time() * 1000), 'text': '', 'status': 'error',
                                     'error': f'页面导航期间弹幕缓冲已满，丢弃{chat_dropped}条'},
                                    f'startup-gap:{generation}')

        try:
            page = await self.browser.page(url, on_danmaku=on_danmaku, on_resolved=on_resolved)
            canonical = room_url(page.url)
            room_id = canonical.rsplit('/', 1)[1]
            async with self.lock:
                if room_id in self.tasks or room_id in self.stopping:
                    raise ValueError('同一实际房间已在监控中')
                if (room_id != chat_context.get('room_id') or
                        self.generations.get(room_id) != chat_context.get('generation')):
                    raise ValueError('房间启动已取消或来源发生变化')
                metadata = await page.evaluate('''() => {
                    const data=window.__NEPTUNE_IS_MY_WAIFU__?.roomInfoRes?.data;
                    return {title:document.title,
                    cover_url:data?.room_info?.cover || data?.anchor_info?.base_info?.face ||
                              document.querySelector('meta[property="og:image"]')?.content ||
                               document.querySelector('video')?.poster || ''}}''')
                if self.generations.get(room_id) != chat_context['generation']:
                    raise ValueError('房间启动已取消')
                room = {'room_id': room_id, 'url': canonical, **metadata, 'chunk_seconds': seconds,
                        'status': 'capturing', 'error': None, 'muted': True,
                        'cycle_notice': False}
                self.store.room(room)
                self.pages[room_id] = page
                generation = chat_context['generation']
                self.tasks[room_id] = asyncio.create_task(self.capture(room, page, generation))
                return room
        except BaseException as error:
            if chat_context:
                rid, generation = chat_context['room_id'], chat_context['generation']
                if self.generations.get(rid) == generation:
                    self.generations.pop(rid, None)
                    room = next((r for r in self.store.rooms() if r['room_id'] == rid), None)
                    if room:
                        room.update(status='error', error=f'启动失败：{type(error).__name__}: {error}')
                        self.store.room(room)
            if page and not page.is_closed():
                try:
                    await close_page(page)
                except Exception as error:
                    self.store.event('cleanup_error', {'error': str(error)})
            raise
        finally:
            self.pending.discard(url)
            if chat_context:
                rid = chat_context['room_id']
                if self.starting_rooms.get(rid) == chat_context['generation']:
                    self.starting_rooms.pop(rid, None)

    async def capture(self, room, page, generation):
        room_id = room['room_id']
        self.failures[room_id] = 0
        try:
            reconnect = False
            while self.generations.get(room_id) == generation:
                try:
                    if reconnect:
                        delay = min(30, self.settings.retry_delay * 2 ** (self.failures[room_id] - 1))
                        await asyncio.sleep(delay)
                        await page.reload(wait_until='domcontentloaded', timeout=30000)
                        await page.wait_for_function(
                            'document.querySelector("video")?.readyState >= 2', timeout=30000)
                        await ensure_playing(page)
                        room.update(status='capturing', error=None)
                        self.store.room(room)
                    await self.capture_cycles(room, page, generation)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.failures[room_id] += 1
                    exhausted = self.failures[room_id] >= self.settings.max_reconnects
                    room.update(status='error' if exhausted else 'reconnecting',
                                error=f'{type(error).__name__}: {error}')
                    self.store.room(room)
                    if exhausted:
                        break
                    reconnect = True
        finally:
            try:
                await close_page(page)
            except Exception as error:
                self.store.event('cleanup_error', {'room_id': room_id, 'error': str(error)})
            finally:
                if self.tasks.get(room_id) is asyncio.current_task():
                    self.tasks.pop(room_id, None)
                if self.pages.get(room_id) is page:
                    self.pages.pop(room_id, None)
                self.failures.pop(room_id, None)
                if self.generations.get(room_id) == generation:
                    self.generations.pop(room_id, None)

    async def capture_cycles(self, room, page, generation):
        room_id = room['room_id']
        seconds = room['chunk_seconds']
        seq = self.store.next_seq(room_id)
        await start_pcm(page)
        try:
            while True:
                pcm, payload = await read_pcm(page, seconds)
                timing = {'start_utc': iso_ms(payload['started']), 'end_utc': iso_ms(payload['ended']),
                          'duration_seconds': len(pcm)/32000,
                          'wall_seconds': (payload['ended']-payload['started'])/1000}
                timing['coverage_ratio'] = (timing['duration_seconds']/timing['wall_seconds']
                                            if timing['wall_seconds'] else 0)
                segment = {'room_id': room_id, 'seq': seq, **timing,
                           'muted': payload['muted'], 'paused': payload['paused'],
                           'track_live': payload['track_live'], 'rms_dbfs': rms_dbfs(pcm),
                           'queue_wait_ms': 0, 'infer_ms': 0, 'text': '', 'error': None,
                           'media_advanced': payload['media_advanced'], 'pauses': payload['pauses'],
                           'missing_samples': payload['missing_samples'],
                           'dropped_samples': payload['dropped_samples'],
                           'track_muted_ms': payload['track_muted_ms']}
                try:
                    window_metadata(pcm, payload['started'], payload['ended'])
                    validate_source(payload)
                    if payload['missing_samples'] > 1600 or payload['dropped_samples']:
                        raise RuntimeError(f'音频输入缺失：{payload["missing_samples"]}样本，'
                                           f'缓冲丢弃：{payload["dropped_samples"]}样本')
                    if payload['media_advanced'] < segment['wall_seconds'] * 0.8:
                        raise RuntimeError(f'播放器进度不足：{payload["media_advanced"]:.2f}秒')
                except Exception as error:
                    segment.update(status='error', error=f'{type(error).__name__}: {error}')
                    self.store.segment(segment)
                    raise
                self.submit(segment, pcm, generation)
                self.failures[room_id] = 0  # 成功采集后重置连续失败预算，不累计终身失败。
                seq += 1
        finally:
            if not page.is_closed():
                try:
                    await close_pcm(page)
                except Exception as error:
                    self.store.event('capture_cleanup_error', {'room_id': room_id, 'error': str(error)})

    def submit(self, segment, pcm, generation):
        if segment['rms_dbfs'] < self.settings.silence_dbfs:
            segment['status'] = 'silent'
            self.store.segment(segment)
            return
        if self.queue.full():
            self.overload_count += 1
            segment.update(status='error', error='queue_overload：识别队列已满，本段已丢弃')
            self.store.segment(segment)
            self.store.event('queue_overload', {'room_id': segment['room_id'], 'count': self.overload_count})
            return
        directory = self.settings.data_dir / 'audio'
        directory.mkdir(exist_ok=True)
        path = directory / f'{uuid.uuid4().hex}.wav'
        with wave.open(str(path), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        segment['status'] = 'queued'
        self.store.segment(segment)
        self.queue.put_nowait((segment, path, time.monotonic(), generation))

    async def consume(self):
        while True:
            segment, path, queued_at, generation = await self.queue.get()
            try:
                if self.generations.get(segment['room_id']) != generation:
                    segment.update(status='cancelled', error='监控已停止，本段未识别')
                else:
                    segment['queue_wait_ms'] = round((time.monotonic() - queued_at) * 1000)
                    segment['status'] = 'transcribing'
                    self.inflight = {'room_id': segment['room_id'], 'seq': segment['seq']}
                    self.store.segment(segment)
                    self.active_generation = generation
                    self.inference_task = asyncio.create_task(
                        self.asr.transcribe(path, f'{generation}:{segment["seq"]}'))
                    result = await self.inference_task
                    if self.generations.get(segment['room_id']) != generation:
                        segment.update(status='cancelled', text='', error='房间已停止，忽略迟到的识别结果')
                        self.store.segment(segment)
                        continue
                    segment.update(text=result.get('text', ''), infer_ms=result.get('infer_ms', 0),
                                   error=result.get('error'))
                    segment['status'] = ('error' if result.get('error') else
                                         'truncated' if result.get('truncated') else
                                         'done' if segment['text'].strip() else 'empty')
                self.store.segment(segment)
            except asyncio.CancelledError:
                segment.update(status='cancelled', error='服务或房间已停止，中断识别')
                self.store.segment(segment)
                if asyncio.current_task().cancelling():
                    raise
            except Exception as error:
                segment.update(status='error', error=f'{type(error).__name__}: {error}')
                self.store.segment(segment)
            finally:
                path.unlink(missing_ok=True)
                self.inflight = None
                self.inference_task = None
                self.active_generation = None
                self.queue.task_done()

    async def stop(self, room_id):
        self.stopping.add(room_id)
        try:
            return await self._stop(room_id)
        finally:
            self.stopping.discard(room_id)

    async def _stop(self, room_id):
        self.generations.pop(room_id, None)
        self.cancel_queued(room_id)
        if self.inflight and self.inflight['room_id'] == room_id and self.inference_task:
            self.inference_task.cancel()
        task = self.tasks.get(room_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.tasks.get(room_id) is task:
            self.tasks.pop(room_id, None)
        room = next((r for r in self.store.rooms() if r['room_id'] == room_id), None)
        if room is None:
            raise KeyError(room_id)
        room['status'] = 'stopped'
        self.store.room(room)
        return room

    def cancel_queued(self, room_id):
        for segment, path, _, _ in self.queue.remove_where(lambda item: item[0]['room_id'] == room_id):
            path.unlink(missing_ok=True)
            segment.update(status='cancelled', error='房间已停止，立即释放队列名额')
            self.store.segment(segment)

    async def close(self):
        for room_id in list(self.tasks):
            await self.stop(room_id)
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.warm_task:
            self.warm_task.cancel()
            await asyncio.gather(self.warm_task, return_exceptions=True)
        while not self.queue.empty():
            segment, path, _, _ = self.queue.get_nowait()
            path.unlink(missing_ok=True)
            segment.update(status='cancelled', error='服务已关闭')
            self.store.segment(segment)
            self.queue.task_done()
        await self.asr.close()
        await self.browser.close()
