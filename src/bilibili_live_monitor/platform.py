"""持久化目标与有界执行层；AI通过候选排序、停止和发现建议异步管理。"""
import asyncio
import shutil
import time
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .browser import room_url
from .retry import backoff_delay
from .room_status import guard_snapshot, room_statuses
from .store import utc_now


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    target_rooms: int = Field(default=0, ge=0)
    record_mode: Literal['none', 'audio', 'video'] = 'audio'
    asr_enabled: bool = False
    preview_enabled: bool = False
    mode: Literal['rotation', 'fixed'] = 'rotation'
    fixed_rooms: list[str] = Field(default_factory=list, max_length=200)

    @field_validator('fixed_rooms')
    @classmethod
    def urls(cls, values):
        return list(dict.fromkeys(room_url(value) for value in values))


class Candidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(max_length=300)
    title: str = Field(default='', max_length=500)
    priority: int = Field(default=0, ge=-1000, le=1000)

    @field_validator('url')
    @classmethod
    def canonical(cls, value):
        return room_url(value)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int
    prioritize: list[str] = Field(default_factory=list, max_length=200)
    stop: list[str] = Field(default_factory=list, max_length=100)
    discover: bool = False
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator('prioritize', 'stop')
    @classmethod
    def urls(cls, values):
        return list(dict.fromkeys(room_url(value) for value in values))


def refused(error):
    return any(marker in error for marker in ('recording_refused', 'discover_refused')) or any(
        f'HTTP {code}' in error for code in (403, 412, 429))


class Platform:
    def __init__(self, monitor):
        self.monitor, self.store, self.settings = monitor, monitor.store, monitor.settings
        saved = self.store.control('platform', {})
        self.policy = Policy.model_validate(saved.get('policy', {}))
        if self.policy.target_rooms > self.settings.max_rooms:
            self.policy.target_rooms = self.settings.max_rooms
        self.revision = saved.get('revision', 0)
        self.candidates = saved.get('candidates', {})
        if self.policy.mode == 'fixed' and not self.policy.fixed_rooms:
            self.policy.fixed_rooms = list(self.candidates)[:self.policy.target_rooms]
        previous = {r['room_id']: r for r in self.store.rooms()}
        for candidate in self.candidates.values():
            old = previous.get(candidate.get('room_id'), {})
            if refused(old.get('error') or '') or 'retry_exhausted:' in (old.get('error') or ''):
                candidate.update(blocked=True, selected=False, last_error=old['error'])
            if self.policy.mode == 'fixed':
                candidate.update(live_status=None, offline_checks=0, watch_state='unknown')
        self.active, self.launching = {}, {}
        self.loop_task = self.discovery_task = self.ai_task = None
        self.closed = False
        self.last_discovery = 0
        self.discovery_requested = False
        self.discovery_blocked = saved.get('discovery_blocked', False)
        self.last_error = None
        self.last_ai = self.store.control('babysitter', {'status': 'not_configured'})
        self.status_task = self.guard_task = None
        self.next_status_check = 0
        self.status_refused = saved.get('status_refused')

    def save(self):
        self.store.save_control('platform', {'policy': self.policy.model_dump(), 'revision': self.revision,
                                            'candidates': self.candidates,
                                            'discovery_blocked': self.discovery_blocked,
                                            'status_refused': self.status_refused})

    def event(self, action, **data):
        self.store.event('platform_action', {'at_utc': utc_now(), 'action': action, **data})

    def configure(self, policy):
        if policy.target_rooms > self.settings.max_rooms:
            raise ValueError('目标超过MAX_ROOMS容量上限')
        if policy.mode == 'fixed':
            if not policy.fixed_rooms:
                policy.fixed_rooms = (list(self.policy.fixed_rooms) if self.policy.mode == 'fixed'
                                      else [c['url'] for c in sorted(self.candidates.values(),
                                            key=lambda c: -c['priority'])[:policy.target_rooms]])
            if any(url not in self.candidates for url in policy.fixed_rooms):
                raise ValueError('固定房间必须先导入候选')
        self.policy = policy
        self.next_status_check = 0
        self.revision += 1
        self.save()
        self.event('policy', revision=self.revision, policy=policy.model_dump())
        return {'accepted': True, 'revision': self.revision, 'policy': policy.model_dump()}

    def import_candidates(self, candidates):
        new = 0
        for candidate in candidates:
            if candidate.url not in self.candidates:
                if len(self.candidates) >= 2000:
                    raise ValueError('候选池上限2000；请先管理现有来源')
                self.candidates[candidate.url] = {**candidate.model_dump(), 'selected': False,
                                                  'blocked': False, 'attempts': 0, 'next_attempt': 0}
                new += 1
        self.save()
        return {'accepted': True, 'added': new, 'total': len(self.candidates)}

    def stop_candidate(self, url):
        url = room_url(url)
        for candidate in self.matching_candidates(url):
            candidate.update(blocked=True, selected=False, last_error='operator_stopped')
        self.revision += 1
        self.save()
        self.event('stop_requested', url=url)
        return {'accepted': True, 'revision': self.revision, 'url': url}

    def stop_room_intent(self, room_id):
        try:
            self.stop_candidate(room_id)
        except KeyError:
            pass

    def matching_candidates(self, value):
        url = room_url(value)
        ids = {url.rsplit('/', 1)[-1]}
        direct = self.candidates.get(url)
        if direct and direct.get('room_id'):
            ids.add(direct['room_id'])
        matches = [c for c in self.candidates.values()
                   if c['url'] == url or c.get('room_id') in ids]
        if not matches:
            raise KeyError(url)
        return matches

    def resolve_room_id(self, value):
        try:
            candidate = self.matching_candidates(value)[0]
            return candidate.get('room_id') or room_url(value).rsplit('/', 1)[-1]
        except KeyError:
            return room_url(value).rsplit('/', 1)[-1]

    def fixed_urls(self):
        # 名额钉在配置时选定的房间；离线/封禁不让池内其他候选接替。
        return set(self.policy.fixed_rooms[:self.policy.target_rooms])

    def resume_candidate(self, url):
        """仅显式操作者请求解除停用/接口拒绝；重新导入不能解锁。"""
        url = room_url(url)
        matches = self.matching_candidates(url)
        ids = {identity for c in matches for identity in (c.get('room_id'), c['url'].rsplit('/', 1)[-1])}
        for candidate in matches:
            candidate.update(blocked=False, selected=False, attempts=0, next_attempt=0,
                             last_error=None, watch_state='unknown', live_status=None,
                             offline_checks=0, guard_refused=None, blocked_alias=False,
                             next_guard_check=0)
        for room in self.store.rooms():
            if room['room_id'] in ids and room['room_id'] not in self.monitor.tasks:
                room['error'] = None
                room.update(reconnect_attempts=0, next_retry_utc=None)
                self.store.room(room)
        self.status_refused = None
        self.next_status_check = 0
        self.revision += 1
        self.save()
        self.event('operator_resumed', url=url)
        return {'accepted': True, 'revision': self.revision, 'url': url}

    async def refresh_statuses(self):
        revision = self.revision
        candidates = [dict(c) for c in self.candidates.values()
                      if not c['blocked'] and c['url'] in self.fixed_urls()]
        if not candidates:
            return
        unresolved_aliases = [dict(c) for c in self.candidates.values()
                              if c['blocked'] and not c.get('room_id')]
        try:
            # 只读解析停用短号的归属，不重新打开或启动这些来源。
            statuses = await room_statuses(self.monitor.browser, candidates + unresolved_aliases)
        except Exception as error:
            if self.revision != revision:
                return
            self.event('status_check_failed', error=str(error))
            # 明确拒绝持久停查；网络异常下轮恢复，不把未知当下播。
            from .rotation import RoomStatusRefused
            if isinstance(error, RoomStatusRefused):
                self.status_refused = str(error)
            for c in candidates:
                self.candidates[c['url']].update(status_error=str(error), offline_checks=0,
                                                  live_status=None, watch_state='unknown')
            self.save()
            return
        if self.revision != revision or self.policy.mode != 'fixed' or not self.policy.enabled:
            return
        for c in unresolved_aliases:
            row = statuses.get(c['url'].rsplit('/', 1)[-1])
            if row:
                self.candidates[c['url']]['room_id'] = str(row['room_id'])
        seen = set()
        blocked_ids = {identity for c in self.candidates.values() if c['blocked']
                       for identity in (c['url'].rsplit('/', 1)[-1], c.get('room_id')) if identity}
        for c in sorted(candidates, key=lambda c: c['url'] not in self.active):
            candidate = self.candidates[c['url']]
            row = statuses.get(c['url'].rsplit('/', 1)[-1]) or statuses.get(c.get('room_id'))
            state = row.get('live_status') if row else None
            candidate.update(live_status=state, status_checked_utc=utc_now(), status_error=None)
            if state not in (0, 1, 2):
                candidate.update(offline_checks=0, watch_state='unknown')
                continue
            canonical = str(row['room_id'])
            candidate.update(room_id=canonical, anchor_uid=str(row.get('uid') or ''),
                             title=row.get('title') or candidate['title'])
            candidate['alias_duplicate'] = canonical in seen
            candidate['blocked_alias'] = canonical in blocked_ids
            seen.add(canonical)
            previous_state = candidate.get('watch_state')
            candidate['offline_checks'] = candidate.get('offline_checks', 0) + 1 if state != 1 else 0
            candidate['watch_state'] = ('attention' if candidate['blocked_alias'] else 'live' if state == 1
                                        else 'confirming_offline' if candidate['offline_checks'] == 1
                                        else 'waiting')
            if not candidate['anchor_uid']:
                candidate['guard_error'] = '状态接口缺少主播UID，无法查询舰队榜单'
            if previous_state != candidate['watch_state']:
                self.event('broadcast_state', url=c['url'], room_id=canonical, live_status=state)
        self.save()

    async def refresh_guards(self):
        for candidate in list(self.candidates.values()):
            if (not self.policy.enabled or self.policy.mode != 'fixed' or candidate['url'] not in self.fixed_urls()
                    or candidate['blocked'] or candidate.get('blocked_alias') or candidate.get('alias_duplicate') or candidate.get('guard_refused')
                    or not candidate.get('anchor_uid') or time.time() < candidate.get('next_guard_check', 0)):
                continue
            candidate['next_guard_check'] = time.time() + self.settings.guard_poll_seconds
            try:
                snapshot = await guard_snapshot(self.monitor.browser, candidate['room_id'], candidate['anchor_uid'])
                self.store.guard_snapshot(snapshot)
                candidate['guard_snapshot'] = {k: snapshot[k] for k in ('observed_utc', 'reported_total', 'complete')}
                candidate['guard_error'] = None
            except Exception as error:
                candidate['guard_error'] = str(error)
                candidate['next_guard_check'] = time.time() + 60
                from .rotation import RoomStatusRefused
                if isinstance(error, RoomStatusRefused):
                    candidate['guard_refused'] = str(error)
                self.event('guard_snapshot_failed', url=candidate['url'], error=str(error))
            self.save()

    def apply_decision(self, decision):
        if decision.revision != self.revision:
            raise ValueError('过期决策：目标或人工操作已变化')
        if any(url not in self.candidates for url in decision.prioritize + decision.stop):
            raise ValueError('决策包含未知候选；须先通过网页发现并导入')
        for index, url in enumerate(decision.prioritize):
            self.candidates[url]['priority'] = 1000-index
        for url in decision.stop:
            self.candidates[url].update(blocked=True, selected=False, last_error='ai_stopped')
        self.discovery_requested |= decision.discover
        self.revision += 1
        self.save()
        self.event('ai_decision', **decision.model_dump(), applied_revision=self.revision)
        return {'accepted': True, 'revision': self.revision}

    def summary(self):
        rooms = self.store.rooms()
        tasks = self.monitor.tasks
        healthy = [r for r in rooms if r['room_id'] in tasks and r['status'] == 'capturing']
        return {'policy': self.policy.model_dump(), 'revision': self.revision,
                'capacity': self.settings.max_rooms, 'active_tasks': len(tasks),
                'launching': len(self.launching), 'pending_pages': len(self.monitor.pending),
                'capturing': len(healthy),
                'recording': sum(r.get('recording_status') == 'recording' for r in healthy),
                'source_seconds': sum(r.get('recorded_seconds', 0) for r in rooms),
                'recording_bytes': sum(r.get('recording_bytes', 0) for r in rooms),
                'room_statuses': dict(Counter(r['status'] for r in rooms)),
                'sources': [{k: r.get(k) for k in ('room_id', 'status', 'recording_status',
                                                  'recorded_seconds', 'error', 'attention', 'updated_at')} for r in rooms],
                'attention_count': sum(bool(r.get('attention')) for r in rooms if r['room_id'] in tasks),
                'free_bytes': shutil.disk_usage(self.settings.data_dir).free,
                'queue_depth': self.monitor.queue.qsize(), 'asr_ready': self.monitor.asr.ready,
                'candidates': list(self.candidates.values()), 'last_error': self.last_error,
                'discovery_blocked': self.discovery_blocked, 'babysitter': self.last_ai,
                'status_refused': self.status_refused,
                'message_health': self.store.control('message_health', {}),
                'event_cursor': self.store.cursor()}

    async def start(self):
        self.loop_task = asyncio.create_task(self.run())
        if self.settings.babysitter_command:
            from .babysitter import babysit
            self.ai_task = asyncio.create_task(babysit(self))

    async def run(self):
        while True:
            try:
                await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = f'{type(error).__name__}: {error}'
                self.event('reconcile_error', error=self.last_error)
            await asyncio.sleep(self.settings.platform_tick_seconds)

    def failed(self, candidate, error):
        candidate.update(selected=False, last_error=error,
                         next_attempt=time.time()+backoff_delay(candidate['attempts'],
                         self.settings.retry_delay, self.settings.retry_max_delay))
        candidate['watch_state'] = 'retrying'
        if refused(error) or 'retry_exhausted:' in error or candidate['attempts'] >= self.settings.max_reconnects:
            candidate['blocked'] = True
            candidate['watch_state'] = 'attention'
        self.save()
        self.event('source_failed', url=candidate['url'], error=error, blocked=candidate['blocked'])

    async def launch(self, candidate):
        candidate['attempts'] += 1
        candidate['selected'] = True
        self.save()
        try:
            old = next((r for r in self.store.rooms() if r['room_id'] == candidate.get('room_id')), {})
            options = self.policy.model_dump(exclude={'enabled', 'target_rooms', 'mode', 'fixed_rooms'})
            room = await self.monitor.add(candidate['url'], self.settings.chunk_seconds, **options)
            candidate['room_id'] = room['room_id']
            candidate['started_epoch'] = time.time()
            candidate['start_seconds'] = room.get('recorded_seconds', 0)
            self.active[candidate['url']] = room['room_id']
            candidate['watch_state'] = 'recording_pending'
            candidate['last_error'] = None
            self.save()
            self.event('source_started', url=candidate['url'], room_id=room['room_id'],
                       previous_source_seconds=old.get('recorded_seconds', 0))
        except asyncio.CancelledError:
            candidate['selected'] = False
            self.save()
            raise
        except Exception as error:
            self.failed(candidate, f'{type(error).__name__}: {error}')

    async def discover(self):
        try:
            candidates = await self.monitor.browser.discover(200)
            self.import_candidates([Candidate(url=c['url'], title=c.get('title', '')) for c in candidates])
            self.event('discovered', count=len(candidates))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if refused(str(error)):
                self.discovery_blocked = True
                self.save()
            self.event('discovery_failed', error=f'{type(error).__name__}: {error}')

    async def tick(self):
        for url, task in list(self.launching.items()):
            if task.done():
                task.result()
                self.launching.pop(url)
        rooms = {r['room_id']: r for r in self.store.rooms()}
        target = self.policy.target_rooms if self.policy.enabled else 0
        fixed = self.policy.mode == 'fixed'
        if fixed and target:
            if self.status_task is None or self.status_task.done():
                if not self.status_refused and time.time() >= self.next_status_check:
                    self.next_status_check = time.time() + self.settings.watch_poll_seconds
                    self.status_task = asyncio.create_task(self.refresh_statuses())
            if self.guard_task is None or self.guard_task.done():
                self.guard_task = asyncio.create_task(self.refresh_guards())
        if shutil.disk_usage(self.settings.data_dir).free < self.settings.platform_min_free_bytes:
            target = 0
            self.event('disk_pressure')
        for url, rid in list(self.active.items()):
            candidate = self.candidates[url]
            current = rooms.get(rid, {})
            if (candidate['attempts'] and current.get('status') == 'capturing'
                    and current.get('reconnect_attempts', 0) == 0
                    and time.time()-candidate.get('started_epoch', time.time()) >= 60
                    and (current.get('record_mode') == 'none' or
                         current.get('recorded_seconds', 0)-candidate.get('start_seconds', 0) >= 60)):
                candidate['attempts'] = 0
                self.save()
            if fixed and candidate.get('offline_checks', 0) >= 2:
                await self.monitor.stop(rid)
                if current:
                    stopped = next(r for r in self.store.rooms() if r['room_id'] == rid)
                    stopped.update(reconnect_attempts=0, next_retry_utc=None)
                    self.store.room(stopped)
                self.active.pop(url)
                candidate.update(selected=False, attempts=0, next_attempt=0, watch_state='waiting')
                self.save()
                self.event('broadcast_finished', url=url, room_id=rid)
            elif rid not in self.monitor.tasks:
                self.active.pop(url)
                self.failed(candidate, rooms.get(rid, {}).get('error') or 'capture_ended')
            elif (rooms.get(rid, {}).get('status') == 'capturing' and
                  rooms.get(rid, {}).get('record_mode') != 'none' and
                  rooms.get(rid, {}).get('recorded_seconds', 0) <= candidate.get('start_seconds', 0) and
                  time.time()-candidate.get('started_epoch', time.time()) > 60):
                await self.monitor.stop(rid)
                self.active.pop(url)
                self.failed(candidate, 'no_media_progress_60s')
        # 先取消多余启动，再停止多余任务；不停止非平台拥有的手工任务。
        excess = max(0, len(self.monitor.tasks)+len(self.launching)-target)
        for url, task in reversed(list(self.launching.items())):
            if excess or self.candidates[url]['blocked'] or (fixed and (
                    url not in self.fixed_urls() or self.candidates[url].get('blocked_alias')
                    or self.candidates[url].get('offline_checks', 0) >= 2)):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.launching.pop(url)
                excess = max(0, excess-1)
        for url, rid in reversed(list(self.active.items())):
            options_changed = any(rooms.get(rid, {}).get(k) != v for k, v in
                                  self.policy.model_dump(exclude={'enabled', 'target_rooms', 'mode', 'fixed_rooms'}).items())
            if (len(self.monitor.tasks)>target or self.candidates[url]['blocked'] or options_changed
                    or (fixed and (url not in self.fixed_urls() or self.candidates[url].get('blocked_alias')))):
                await self.monitor.stop(rid)
                self.active.pop(url)
                self.candidates[url]['selected'] = False
                self.save()
                self.event('source_stopped', url=url, room_id=rid)
        slots = min(target-len(self.monitor.tasks)-len(self.launching),
                    self.settings.platform_start_concurrency-len(self.launching))
        eligible = sorted((c for url, c in self.candidates.items() if not c['blocked'] and
                            url not in self.active and url not in self.launching and
                            (not fixed or (url in self.fixed_urls() and c.get('live_status') == 1
                                           and not c.get('alias_duplicate') and not c.get('blocked_alias')
                                           and not self.status_refused)) and
                           c['next_attempt'] <= time.time()),
                          key=lambda c: (not c['selected'], -c['priority']))
        for candidate in eligible[:max(0, slots)]:
            self.launching[candidate['url']] = asyncio.create_task(self.launch(candidate))
        if (not fixed and target and not self.discovery_blocked and
                (self.discovery_requested or len(eligible)<target) and
                time.monotonic()-self.last_discovery >= 300 and
                (self.discovery_task is None or self.discovery_task.done())):
            self.last_discovery = time.monotonic()
            self.discovery_requested = False
            self.discovery_task = asyncio.create_task(self.discover())

    async def close(self):
        self.closed = True
        tasks = [t for t in (self.loop_task, self.discovery_task, self.ai_task, self.status_task,
                            self.guard_task, *self.launching.values()) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.save()  # 保留policy与目标候选；Monitor.close只关闭本次执行，不撤销目标。
