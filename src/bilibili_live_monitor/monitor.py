import asyncio
import math
import time
import uuid
import wave
from datetime import datetime, timezone

import numpy as np

from .asr import StreamingASR
from .audio_queue import AudioQueue
from .browser import Browser, close_page, ensure_playing, room_url
from .capture import close_pcm, read_pcm, start_pcm
from .pcm_recording import PCMRecording
from .preview import Previews
from .recording import Recording
from .retry import backoff_delay, remaining_delay

PAGE_INSPECTION_SCRIPT = '''pause => {
    if(pause&&!window.__liveMonitorPauseGuard){
        const stop=()=>document.querySelectorAll('video').forEach(v=>{if(!v.paused)v.pause()});
        document.addEventListener('play',stop,true);
        new MutationObserver(stop).observe(document,{childList:true,subtree:true});
        window.__liveMonitorPauseGuard=true;
    }
    if(pause){
        let style=document.getElementById('live-monitor-low-render');
        if(!style){style=document.createElement('style');style.id='live-monitor-low-render';
            document.documentElement.append(style)}
        // 根节点opacity不继承，保留子元素验证码visibility/opacity判据与页面布局。
        // 不冻结全局动画：验证码可能靠淡入动画从opacity=0变为可检测。
        if(style.textContent!=='html{opacity:0!important}')style.textContent='html{opacity:0!important}';
    }
    if(pause)document.querySelectorAll('video').forEach(v=>{if(!v.paused)v.pause()});
    const selectors='.geetest_panel_box,.geetest_panel,iframe[src*="captcha"],'
        +'iframe[src*="passport"],.bili-mini-login,input[placeholder*="验证码"]';
    const visible=[...document.querySelectorAll(selectors)].some(e=>{
        const r=e.getBoundingClientRect(),s=getComputedStyle(e);
        return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.opacity!=='0';});
    return {attention:visible?'verification_or_login':null};
}'''


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
    if payload.get('source_changed'):
        raise RuntimeError('播放器元素已更换，需要重建采集')
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
        self.asr = StreamingASR(settings)
        self.stream_rows = {}
        self.stream_inputs = {}
        self.pending_finishes = set()
        self.queue = AudioQueue(maxsize=settings.queue_size)
        self.tasks, self.pages, self.generations = {}, {}, {}
        self.pending = set()
        self.stopping = set()
        self.stop_tasks = {}
        self.starting_rooms = {}
        self.lock = asyncio.Lock()
        self.worker = None
        self.overload_count = 0
        self.inflight = None
        self.inference_task = None
        self.active_generation = None
        self.failures = {}
        self.warm_task = None
        self.previews = Previews(lambda rid: self.pages.get(rid) if any(
            r['room_id'] == rid and r.get('preview_enabled') for r in self.store.rooms()) else None)

    async def start(self, *, warm=True):
        directory = self.settings.data_dir / 'audio'
        if directory.exists() and self.queue.empty():
            for path in directory.glob('*.wav'):
                if len(path.stem) == 32 and all(c in '0123456789abcdef' for c in path.stem):
                    path.unlink(missing_ok=True)
        if warm and self.settings.asr_enabled:
            self.warm_task = asyncio.create_task(self.warm_model())
        if self.settings.asr_enabled:
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
            room['like_summary'] = self.store.like_summary(room['room_id'])
        return {'rooms': rooms, **self.metrics(), 'cursor': self.store.cursor()}

    def metrics(self):
        return {'asr_enabled': self.settings.asr_enabled or any(
                    r.get('asr_enabled') and r['room_id'] in self.tasks for r in self.store.rooms()),
                'record_mode': self.settings.record_mode, 'preview_enabled': self.settings.preview_enabled,
                'queue_depth': self.queue.qsize(), 'queue_capacity': self.queue.maxsize,
                'asr_ready': self.asr.ready, 'overload_count': self.overload_count,
                'inflight': self.inflight,
                'active_count': len(self.tasks), 'pending_count': len(self.pending),
                'asr_restarts': self.asr.restart_count, 'asr_retries': self.asr.retry_count,
                'asr_error': self.asr.last_error}

    async def add(self, value, seconds, *, record_mode=None, asr_enabled=None, preview_enabled=None,
                  retry_attempts=0):
        seconds = 5  # 实时采音固定每5秒送入流式上下文；旧API参数不再控制独立切片。
        options = {'record_mode': self.settings.record_mode if record_mode is None else record_mode,
                   'asr_enabled': self.settings.asr_enabled if asr_enabled is None else asr_enabled,
                   'preview_enabled': self.settings.preview_enabled if preview_enabled is None else preview_enabled}
        if options['record_mode'] not in ('none', 'audio', 'video'):
            raise ValueError('保存模式无效')
        url = room_url(value)
        async with self.lock:
            if url in self.pending or url.rsplit('/', 1)[1] in self.tasks or url.rsplit('/', 1)[1] in self.stopping:
                raise ValueError('该房间已在监控或启动中')
            if len(self.tasks) + len(self.pending) >= self.settings.max_rooms:
                raise ValueError('已达到同时监控上限')
            self.pending.add(url)
        page = None
        chat_context, chat_buffer = {}, []
        previous = {}
        chat_dropped = 0

        def on_danmaku(message):
            nonlocal chat_dropped
            if not chat_context:
                if len(chat_buffer) < 200:
                    chat_buffer.append(message)
                else:
                    chat_dropped += 1
            elif self.generations.get(chat_context['room_id']) == chat_context['generation']:
                self.store.live_message(chat_context['room_id'], {**message, 'session_id': chat_context['generation']})

        async def on_resolved(resolved_page):
            room_id = room_url(resolved_page.url).rsplit('/', 1)[1]
            async with self.lock:
                if room_id in self.tasks or room_id in self.stopping or room_id in self.starting_rooms:
                    raise ValueError('同一实际房间已在监控或启动中')
                generation = uuid.uuid4().hex
                self.starting_rooms[room_id] = generation
                self.generations[room_id] = generation
                chat_context.update(room_id=room_id, generation=generation)
                previous.update(next((r for r in self.store.rooms() if r['room_id'] == room_id), {}))
                self.store.room({**previous, 'room_id': room_id, 'url': room_url(resolved_page.url),
                                 'source_url': url,
                                 'title': room_id, 'cover_url': '',
                                   'chunk_seconds': seconds, 'status': 'reconnecting',
                                   'reconnect_attempts': max(previous.get('reconnect_attempts', 0), retry_attempts),
                                  'error': None, 'muted': True, 'cycle_notice': False, **options})
                for message in chat_buffer:
                    on_danmaku(message)
                chat_buffer.clear()
                if chat_dropped:
                    self.store.item({'room_id': room_id, 'kind': 'danmaku',
                                     'at_utc': iso_ms(time.time() * 1000), 'text': '', 'status': 'error',
                                     'error': f'页面导航期间直播事件缓冲已满，丢弃{chat_dropped}条（可能含礼物）'},
                                    f'startup-gap:{generation}')

        try:
            saved = next((r for r in self.store.rooms() if r.get('url') == url or r.get('source_url') == url or
                          r['room_id'] == url.rsplit('/', 1)[-1]), {})
            if delay := remaining_delay(saved.get('next_retry_utc')):
                await asyncio.sleep(delay)
            page = await self.browser.page(url, on_danmaku=on_danmaku, on_resolved=on_resolved,
                                           require_playback=options['asr_enabled'] or options['preview_enabled'] or options['record_mode'] == 'audio')
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
                room = {'room_id': room_id, 'url': canonical, 'source_url': url, **metadata, 'chunk_seconds': seconds,
                         'status': 'capturing', 'error': None, 'muted': True,
                         'cycle_notice': False, **options, 'recording_status': 'disabled' if options['record_mode'] == 'none' else 'starting',
                           'recorded_seconds': previous.get('recorded_seconds', 0),
                           'recording_files': previous.get('recording_files', 0),
                           'recording_bytes': previous.get('recording_bytes', 0),
                           'reconnect_attempts': max(previous.get('reconnect_attempts', 0), retry_attempts),
                           'next_retry_utc': previous.get('next_retry_utc')}
                if previous.get('recording_session'):
                    room['recording_session'] = previous['recording_session']
                self.store.room(room)
                self.pages[room_id] = page
                generation = chat_context['generation']
                if room['asr_enabled'] and self.worker is None:
                    self.worker = asyncio.create_task(self.consume())
                    self.warm_task = asyncio.create_task(self.warm_model())
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
        self.failures[room_id] = room.get('reconnect_attempts', 0)
        try:
            if delay := remaining_delay(room.get('next_retry_utc')):
                await asyncio.sleep(delay)
            reconnect = False
            while self.generations.get(room_id) == generation:
                try:
                    if reconnect:
                        delay = backoff_delay(self.failures[room_id], self.settings.retry_delay,
                                              self.settings.retry_max_delay)
                        await asyncio.sleep(delay)
                        if page.is_closed():
                            if self.pages.get(room_id) is page:
                                self.pages.pop(room_id, None)
                            buffered, validated = [], False
                            dropped = 0
                            def on_message(message):
                                nonlocal dropped
                                if self.generations.get(room_id) == generation:
                                    if validated:
                                        self.store.live_message(room_id, {**message, 'session_id': generation})
                                    elif len(buffered) < 200:
                                        buffered.append(message)
                                    else:
                                        dropped += 1
                            replacement = await self.browser.page(room['url'], on_danmaku=on_message,
                                require_playback=room.get('asr_enabled', True) or room.get('preview_enabled') or room.get('record_mode') == 'audio')
                            try:
                                if room_url(replacement.url).rsplit('/', 1)[-1] != room_id:
                                    raise RuntimeError('恢复页面跳转到其他房间')
                            except BaseException:
                                await close_page(replacement)
                                raise
                            page = replacement
                            self.pages[room_id] = page
                            validated = True
                            for message in buffered:
                                on_message(message)
                            buffered.clear()
                            if dropped:
                                self.store.event('reconnect_message_gap', {'room_id': room_id, 'dropped': dropped})
                        else:
                            page.recording_url = None
                            page.chat_last_frame = time.monotonic()
                            response = await page.reload(wait_until='domcontentloaded', timeout=30000)
                            if response and response.status in (403, 412, 429):
                                raise RuntimeError(f'recording_refused: HTTP {response.status}')
                            if room.get('asr_enabled', True) or room.get('preview_enabled') or room.get('record_mode') == 'audio':
                                await page.wait_for_function(
                                    'document.querySelector("video")?.readyState >= 2', timeout=30000)
                                await ensure_playing(page)
                        room.update(status='capturing', error=None, next_retry_utc=None)
                        self.store.room(room)
                    await self.session_cycles(room, page, generation)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.failures[room_id] += 1
                    exhausted = ('recording_refused' in str(error) or
                                  self.failures[room_id] >= self.settings.max_reconnects)
                    delay = backoff_delay(self.failures[room_id], self.settings.retry_delay,
                                          self.settings.retry_max_delay)
                    reason = f'{type(error).__name__}: {error}'
                    if exhausted and 'recording_refused' not in str(error):
                        reason = f'retry_exhausted: 连续失败{self.failures[room_id]}次；{reason}'
                    room.update(status='error' if exhausted else 'reconnecting',
                                reconnect_attempts=self.failures[room_id],
                                next_retry_utc=None if exhausted else iso_ms((time.time()+delay)*1000),
                                error=reason)
                    self.store.room(room)
                    if exhausted:
                        break
                    reconnect = True
        finally:
            try:
                self.store.live_message(room_id, {'kind': 'transport', 'state': 'disconnected',
                                                 'session_id': generation})
                await self.previews.stop(room_id)
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

    async def session_cycles(self, room, page, generation):
        recorder = None
        audio_task = None
        healthy_started = time.monotonic()
        reset_budget = False
        last_store = 0
        if room.get('record_mode') == 'audio':
            recorder = PCMRecording(self.settings, room)
            audio_task = asyncio.create_task(self.capture_cycles(room, page, generation, recorder))
        elif room.get('asr_enabled', True):
            audio_task = asyncio.create_task(self.capture_audio(room, page, generation))
        try:
            await self.inspect_page(room, page)
            if room.get('record_mode', 'none') == 'video':
                for _ in range(60):
                    if getattr(page, 'recording_url', None):
                        break
                    await asyncio.sleep(.5)
                if not getattr(page, 'recording_url', None):
                    await self.inspect_page(room, page)
                    raise RuntimeError('recording_url_missing：未发现网页正在请求的直播流')
                recorder = Recording(self.settings, room)
                await recorder.start(page.recording_url)
            while True:
                if time.monotonic() - getattr(page, 'chat_last_frame', time.monotonic()) > 120:
                    raise RuntimeError('chat_stalled：120秒无弹幕连接帧或心跳，重新建立页面连接')
                if recorder:
                    await recorder.check()
                    if time.monotonic()-last_store >= 10:
                        room.update(recorder.snapshot())
                        self.store.room(room)
                        last_store = time.monotonic()
                if audio_task and audio_task.done():
                    await audio_task
                    raise RuntimeError('audio_capture_ended')
                if (recorder is not None and not reset_budget and time.monotonic()-healthy_started >= 60 and
                        recorder.progress_seconds >= 60):
                    self.failures[room['room_id']] = 0
                    room.update(reconnect_attempts=0, next_retry_utc=None)
                    self.store.room(room)
                    reset_budget = True
                if not room.get('asr_enabled', True) and not room.get('preview_enabled') and room.get('record_mode') != 'audio':
                    # 保留网页弹幕WebSocket，暂停本地视频解码；录制由ffmpeg独立拉流。
                    await self.inspect_page(room, page, pause=True)
                if audio_task:
                    await asyncio.wait({audio_task}, timeout=3)
                else:
                    await asyncio.sleep(3)
        finally:
            async def cleanup():
                try:
                    if audio_task:
                        audio_task.cancel()
                        await asyncio.gather(audio_task, return_exceptions=True)
                finally:
                    if recorder:
                        await recorder.close()
                        room.update(recorder.snapshot(), recording_status='stopped')
                        self.store.room(room)
            cleanup_task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise

    async def inspect_page(self, room, page, *, pause=False):
        result = await page.evaluate(PAGE_INSPECTION_SCRIPT, pause)
        attention = result.get('attention') if isinstance(result, dict) else None
        if pause:
            # 赛事等页面把播放器放在iframe内；主文档可能完全没有video。
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                try:
                    child = await asyncio.wait_for(frame.evaluate(PAGE_INSPECTION_SCRIPT, True), 5)
                    if isinstance(child, dict):
                        attention = attention or child.get('attention')
                except Exception:
                    # 广告/播放器iframe可能正在导航或已销毁，不因此中断独立录像。
                    continue
        if attention != room.get('attention'):
            room['attention'] = attention
            self.store.room(room)
            self.store.event('platform_action', {'at_utc': iso_ms(time.time()*1000),
                             'action': 'human_attention', 'room_id': room['room_id'], 'attention': attention})

    async def capture_audio(self, room, page, generation):
        failures = 0
        while True:
            try:
                await self.capture_cycles(room, page, generation)
                raise RuntimeError('audio_capture_ended')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if room.get('record_mode', 'none') == 'none':
                    raise
                # 音频采集与独立ffmpeg录像分开恢复；播放器更换不再截断录像。
                failures += 1
                room['audio_error'] = f'{type(error).__name__}: {error}'[:500]
                self.store.room(room)
                self.store.event('audio_capture_retry', {'room_id': room['room_id'],
                                                        'attempt': failures, 'error': room['audio_error']})
                await asyncio.sleep(min(30, 2 ** min(failures - 1, 5)))
                try:
                    await ensure_playing(page)
                except Exception:
                    continue

    async def capture_cycles(self, room, page, generation, recorder=None):
        room_id = room['room_id']
        seconds = 5
        stream_id = uuid.uuid4().hex
        offset = 0
        origin_ms = None
        pending_read = None
        await start_pcm(page)
        try:
            while True:
                if recorder:
                    # 取消Python等待不能取消浏览器evaluate；保留在途结果，避免丢掉已取走的PCM。
                    pending_read = asyncio.create_task(read_pcm(page, seconds))
                    pcm, payload = await asyncio.shield(pending_read)
                    pending_read = None
                else:
                    pcm, payload = await read_pcm(page, seconds)
                if recorder:
                    # 保存实际收到的音频；质量检查控制ASR/重连，不删除暂停或断流前的这一窗。
                    recorder.write(pcm)
                if origin_ms is None:
                    origin_ms = payload['started']
                timing = {'start_utc': iso_ms(payload['started']), 'end_utc': iso_ms(payload['ended']),
                          'duration_seconds': len(pcm)/32000,
                          'wall_seconds': (payload['ended']-payload['started'])/1000}
                timing['coverage_ratio'] = (timing['duration_seconds']/timing['wall_seconds']
                                            if timing['wall_seconds'] else 0)
                segment = {'room_id': room_id, **timing,
                           'stream_id': stream_id, 'sample_offset': offset, 'origin_ms': origin_ms,
                           'session_id': generation, 'transcript_version': 'realtime',
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
                    self.stream_error(segment, segment['error'])
                    raise
                if room.get('asr_enabled', True):
                    self.submit(segment, pcm, generation)
                if room.pop('audio_error', None):
                    self.store.room(room)
                offset += len(pcm)//2
                if room.get('record_mode', 'none') == 'none':
                    # 短暂恢复不清零，连续有效采集60秒才重置失败预算。
                    if offset >= 16000*60 and self.failures[room_id]:
                        self.failures[room_id] = 0
                        room.update(reconnect_attempts=0, next_retry_utc=None)
                        self.store.room(room)
        finally:
            if recorder and not page.is_closed():
                if pending_read is not None:
                    try:
                        pcm, _ = await pending_read
                        recorder.write(pcm)
                    except Exception as error:
                        self.store.event('recording_read_error', {'room_id': room_id, 'error': str(error)})
                try:
                    pcm, _ = await asyncio.wait_for(read_pcm(page, 0), 4)
                    recorder.write(pcm)
                except Exception as error:
                    self.store.event('recording_tail_error', {'room_id': room_id, 'error': str(error)})
            self.finish_stream(stream_id, generation)
            if not page.is_closed():
                try:
                    await close_pcm(page)
                except Exception as error:
                    self.store.event('capture_cleanup_error', {'room_id': room_id, 'error': str(error)})

    def submit(self, segment, pcm, generation):
        segment['session_id'] = generation
        self.stream_inputs[segment['stream_id']] = segment
        if self.queue.full():
            self.overload_count += 1
            segment.update(status='error', error='queue_overload：识别队列已满，本段已丢弃')
            self.stream_error(segment, segment['error'])
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
        self.queue.put_nowait((segment, path, time.monotonic(), generation))

    def finish_stream(self, stream_id, generation):
        if stream_id in self.pending_finishes or stream_id not in self.stream_inputs:
            return
        self.pending_finishes.add(stream_id)
        self.queue.put_control(({**self.stream_inputs[stream_id], 'op': 'finish'},
                                None, time.monotonic(), generation))

    def stream_error(self, segment, error):
        self.store.segment({**segment, 'seq': self.store.next_seq(segment['room_id']),
                            'status': 'error', 'error': error, 'is_final': True,
                            'transcript_version': 'realtime'})

    def publish_stream(self, segment, result):
        sid = segment['stream_id']
        # worker重启后，上一次还未定稿的行显式中断，不与新上下文拼接。
        for key, row in list(self.stream_rows.items()):
            if key[0] == sid and key[1] != result['epoch']:
                self.store.segment({**row, 'status': 'cancelled', 'is_final': True,
                                    'error': '流式上下文已重建'})
                self.stream_rows.pop(key)
        for update in result.get('updates', []):
            key = (sid, result['epoch'], update['window_id'])
            previous = self.stream_rows.get(key)
            start = iso_ms(segment['origin_ms'] + update['start_seconds']*1000)
            end = iso_ms(segment['origin_ms'] + update['end_seconds']*1000)
            final = update['final']
            row = {**(previous or {}), **segment, 'seq': previous['seq'] if previous else self.store.next_seq(segment['room_id']),
                   'start_utc': start, 'end_utc': end, 'text': update['text'],
                   'stable_text': update['stable_text'], 'is_final': final,
                   'transcript_version': 'realtime', 'infer_ms': result.get('infer_ms', 0),
                   'duration_seconds': update['end_seconds']-update['start_seconds'],
                   'status': ('done' if update['text'].strip() else 'empty') if final else 'transcribing',
                   'error': None}
            # 捕获质量指标属于单次5秒输入，不冒充整个流式窗口的覆盖率。
            for field in ('wall_seconds', 'coverage_ratio', 'media_advanced'):
                row.pop(field, None)
            self.store.segment(row)
            if final:
                self.stream_rows.pop(key, None)
            else:
                self.stream_rows[key] = row

    async def consume(self):
        while True:
            segment, path, queued_at, generation = await self.queue.get()
            try:
                if path is None:
                    result = await self.asr.finish(segment['stream_id'])
                    if self.generations.get(segment['room_id']) == generation and result.get('updates'):
                        self.publish_stream(segment, result)
                    self.interrupt_streams(segment['stream_id'])
                    continue
                if self.generations.get(segment['room_id']) != generation:
                    continue
                else:
                    segment['queue_wait_ms'] = round((time.monotonic() - queued_at) * 1000)
                    segment['status'] = 'transcribing'
                    self.inflight = {'room_id': segment['room_id'], 'seq': self.store.next_seq(segment['room_id'])}
                    self.active_generation = generation
                    self.inference_task = asyncio.create_task(
                        self.asr.feed(path, f'{segment["stream_id"]}:{segment["sample_offset"]}',
                                      segment['stream_id'], segment['sample_offset']))
                    result = await self.inference_task
                    if self.generations.get(segment['room_id']) != generation:
                        continue
                    self.publish_stream(segment, result)
            except asyncio.CancelledError:
                self.interrupt_streams(segment['stream_id'])
                if asyncio.current_task().cancelling():
                    raise
            except Exception as error:
                if path is not None:
                    self.stream_error(segment, f'{type(error).__name__}: {error}')
                self.interrupt_streams(segment['stream_id'])
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)
                else:
                    self.pending_finishes.discard(segment['stream_id'])
                    self.stream_inputs.pop(segment['stream_id'], None)
                self.inflight = None
                self.inference_task = None
                self.active_generation = None
                self.queue.task_done()

    def interrupt_streams(self, stream_id=None, room_id=None):
        for key, row in list(self.stream_rows.items()):
            if key[0] == stream_id or (room_id is not None and row['room_id'] == room_id):
                self.store.segment({**row, 'status': 'cancelled', 'is_final': True,
                                    'error': '采集停止或上下文中断；保留最后一次实时稿'})
                self.stream_rows.pop(key)

    async def stop(self, room_id):
        if room_id not in self.stop_tasks:
            self.stopping.add(room_id)
            self.generations.pop(room_id, None)  # 首次停止立即失效，禁止迟到结果抢在清理任务前发布。
            self.stop_tasks[room_id] = asyncio.create_task(self._stop_once(room_id))
        return await asyncio.shield(self.stop_tasks[room_id])

    async def _stop_once(self, room_id):
        self.stopping.add(room_id)
        try:
            return await self._stop(room_id)
        finally:
            self.stopping.discard(room_id)
            self.stop_tasks.pop(room_id, None)

    async def _stop(self, room_id):
        self.generations.pop(room_id, None)
        self.cancel_queued(room_id)
        self.interrupt_streams(room_id=room_id)
        # 已发送的共享worker请求允许完成；generation已失效，结果不会再发布。
        # 主动停止一个房间不应销毁其他房间的模型上下文。
        task = self.tasks.get(room_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.tasks.get(room_id) is task:
            self.tasks.pop(room_id, None)
        for sid, segment in list(self.stream_inputs.items()):
            if segment['room_id'] == room_id:
                self.finish_stream(sid, segment.get('session_id'))
        room = next((r for r in self.store.rooms() if r['room_id'] == room_id), None)
        if room is None:
            raise KeyError(room_id)
        room['status'] = 'stopped'
        self.store.room(room)
        return room

    def cancel_queued(self, room_id):
        for segment, path, _, _ in self.queue.remove_where(
                lambda item: item[0]['room_id'] == room_id and item[1] is not None):
            if path is not None:
                path.unlink(missing_ok=True)

    async def close(self):
        await self.previews.close()
        await asyncio.gather(*(self.stop(room_id) for room_id in list(self.tasks)))
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.warm_task:
            self.warm_task.cancel()
            await asyncio.gather(self.warm_task, return_exceptions=True)
        while not self.queue.empty():
            segment, path, _, _ = self.queue.get_nowait()
            if path is not None:
                path.unlink(missing_ok=True)
            self.queue.task_done()
        for key in list(self.stream_rows):
            self.interrupt_streams(key[0])
        self.stream_inputs.clear()
        self.pending_finishes.clear()
        await self.asr.close()
        await self.browser.close()
