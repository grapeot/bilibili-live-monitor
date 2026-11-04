"""持久房间自动化：跨场守候、明确外发开关和账号隔离。"""
import asyncio
import hashlib
import html
import json
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .likes import trigger_likes
from .room_config import capture_overrides, reload_plan, validate_rooms
from .welcome import WelcomeDecision, correct_page, message_limit, observations, send_chat, welcome_messages
from .welcome_variants import choose_variant, load_variants, variant_messages


def confirmed_state(candidate, now=None):
    now = time.time() if now is None else now
    try:
        checked = datetime.fromisoformat(candidate['status_checked_utc']).timestamp()
    except (KeyError, ValueError, TypeError):
        return None
    if not 0 <= now-checked <= 75 or candidate.get('status_error'):
        return None
    if candidate.get('live_status') == 1:
        return 'live'
    if candidate.get('live_status') in (0, 2) and candidate.get('offline_checks', 0) >= 2:
        return 'offline'
    return None


class Automation:
    def __init__(self, platform, accounts, config):
        validate_rooms(config.get('rooms', {}))
        self.config = deepcopy(config)
        self.platform, self.accounts = platform, accounts
        self.store, self.browser = platform.store, platform.monitor.browser
        self.rooms = deepcopy(config.get('rooms', {}))
        self.variants = {rid: load_variants(cfg) for rid, cfg in self.rooms.items()}
        self.notification_command = config.get('notification_command', [])
        self.state = self.store.control('automation', {'rooms': {}, 'notifications': {}, 'likes': {}})
        self.tasks = {}
        self.lock = asyncio.Lock()
        self.loop = None
        from .session_workflow import SessionWorkflows
        self.workflows = SessionWorkflows(self, config.get('session_workflow', {}))
        self.platform.session_workflow_rooms = set(self.rooms) if self.workflows.enabled else set()
        self.platform.room_capture_overrides = capture_overrides(self.rooms)
        for row in self.state['likes'].values():
            if row['status'] in ('running', 'queued'):
                row['status'] = 'interrupted'
        for row in self.state['notifications'].values():
            if row['status'] == 'sending':
                row['status'] = 'unknown'
        for row in self.state['rooms'].values():
            if row.get('delivery') == 'sending':
                row['delivery'] = 'ambiguous'
        for rid, variants in self.variants.items():
            if not variants:
                self.row(rid).pop('copy_rotation', None)
                self.row(rid).pop('variant_id', None)
        self.save()

    def save(self):
        self.store.save_control('automation', self.state)

    def row(self, room_id):
        if room_id not in self.rooms:
            raise KeyError('不在自动化房间配置中')
        return self.state['rooms'].setdefault(room_id, {
            'enabled': self.rooms[room_id].get('welcome_enabled', False), 'revision': 0, 'confirmed': None, 'session': None,
            'seen': [], 'cursor': 0, 'last_sent': None, 'pending': [], 'delivery': 'idle'})

    def candidate(self, room_id):
        if not self.platform.policy.enabled or self.platform.policy.mode != 'fixed':
            return {}
        return next((c for c in self.platform.candidates.values()
                     if c['url'] in self.platform.fixed_urls() and
                     (c.get('room_id') or c['url'].rsplit('/', 1)[-1]) == room_id), {})

    def allowed(self, room_id, epoch):
        candidate = self.candidate(room_id)
        return (not self.accounts.switching and self.accounts.epoch == epoch and
                self.accounts.identity['logged_in'] and confirmed_state(candidate) == 'live'
                and not candidate.get('blocked') and not candidate.get('blocked_alias'))

    def summary(self):
        return {'account': self.accounts.summary(), 'rooms': [
            {'room_id': rid, 'label': cfg['label'], 'intro': cfg['intro'],
             'variant_count': len(self.variants[rid]),
             **{k: v for k, v in self.row(rid).items() if k not in ('seen', 'pending')},
             'welcome_running': 'welcome:'+rid in self.tasks}
            for rid, cfg in self.rooms.items()],
            'notifications': list(self.state['notifications'].values())[-20:],
            'session_workflows': list(self.workflows.records.values())[-20:],
            'workflow_errors': self.state.get('workflow_errors', {}),
            'likes': list(self.state['likes'].values())[-20:]}

    async def set_enabled(self, room_id, enabled):
        row = self.row(room_id)
        row.update(enabled=enabled, revision=row['revision']+1, pending=[])
        self.save()  # 撤销立即生效；网络guard会在实际发送前再检查。
        task = self.tasks.get('welcome:'+room_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.summary()

    def prepare_room_configs(self, rooms):
        """校验后返回独立配置；扩展文案模块可在此应用已持久化文案。"""
        return deepcopy(rooms)

    async def reload_rooms(self, config):
        config = reload_plan(self.config, config)
        rooms = self.prepare_room_configs(config.get('rooms', {}))
        added = sorted(set(rooms) - set(self.rooms))
        cancelled = []
        # No await before replacing all references and revoking changed sends.
        # Existing session records retain their frozen URL and workspace.
        self.rooms = rooms
        self.workflows.config = deepcopy(config.get('session_workflow', {}))
        self.platform.session_workflow_rooms = set(rooms) if self.workflows.enabled else set()
        self.platform.room_capture_overrides = capture_overrides(rooms)
        for rid, cfg in rooms.items():
            row = self.row(rid)
            desired = cfg.get('welcome_enabled')
            if desired is not None and desired != row['enabled']:
                row.update(enabled=desired, revision=row['revision']+1, pending=[])
                task = self.tasks.get('welcome:'+rid)
                if task:
                    task.cancel()
                    cancelled.append(task)
        self.config = config
        self.save()
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        await self.start()
        return {'accepted': True, 'added': added, 'rooms': sorted(rooms)}

    async def start(self):
        if self.rooms and (self.loop is None or self.loop.done()):
            self.loop = asyncio.create_task(self.run())

    async def run(self):
        next_identity = 0
        next_browser_attempt = 0
        while True:
            try:
                if self.accounts.switching:
                    await asyncio.sleep(.5)
                    continue
                if (self.accounts.profiles and (self.accounts.process is None
                        or self.accounts.process.poll() is not None)):
                    if time.monotonic() < next_browser_attempt:
                        await asyncio.sleep(1)
                        continue
                    next_browser_attempt = time.monotonic()+30
                    async with self.lock:
                        # 等待锁时可能已经由显式账号切换恢复，不能再启动第二个Chrome。
                        if self.accounts.process is None or self.accounts.process.poll() is not None:
                            await self.accounts.close()
                            await self.accounts.start()
                if time.monotonic() >= next_identity:
                    try:
                        await self.accounts.refresh(self.browser)
                    except Exception:
                        self.accounts.identity['logged_in'] = False
                    next_identity = time.monotonic()+10
                for key, task in list(self.tasks.items()):
                    if task.done():
                        task.exception() if not task.cancelled() else None
                        self.tasks.pop(key)
                for rid in self.rooms:
                    self.observe_state(rid)
                    self.consume(rid)
                    self.schedule_welcome(rid)
                for key, notification in self.state['notifications'].items():
                    if notification['status'] == 'pending' and 'mail:'+key not in self.tasks:
                        self.tasks['mail:'+key] = asyncio.create_task(self.send_notification(key))
                if 'workflow:tick' not in self.tasks and self.workflows.enabled:
                    self.tasks['workflow:tick'] = asyncio.create_task(self.workflows.tick())
                self.save()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.state['error'] = f'{type(error).__name__}: {error}'
                self.save()
            await asyncio.sleep(1)

    def observe_state(self, rid):
        row = self.row(rid)
        candidate = self.candidate(rid)
        current = confirmed_state(candidate)
        start = candidate.get('live_start') or ''
        changed_session = current == 'live' and start and row.get('live_start') and start != row['live_start']
        if current is None or (current == row['confirmed'] and not changed_session):
            if current == 'live' and start:
                row['live_start'] = start
            return
        previous = row['confirmed']
        if current == 'live':
            row = dict(row)
            self.state['rooms'][rid] = row
        row['confirmed'] = current
        if current == 'live':
            row.update(session=uuid.uuid4().hex, seen=[], pending=[], delivery='idle',
                       since=datetime.now(timezone.utc).isoformat(), live_start=start)
            row['revision'] += 1  # 旧场正在执行的发送必须失效。
            row['cursor'] = self.store.db.execute(
                'SELECT COALESCE(MAX(id),0) FROM raw_events WHERE room_id=?', (rid,)).fetchone()[0]
        # 初次已离线只是基线；首次观察到在线则发“检测到开播”。
        if current == 'live' or previous == 'live':
            key = hashlib.sha256(f'{rid}:{row["session"]}:{current}'.encode()).hexdigest()
            try:
                live_start = datetime.strptime(row.get('live_start', ''), '%Y-%m-%d %H:%M:%S').replace(
                    tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc).isoformat()
            except ValueError:
                live_start = row.get('live_start', '')
            self.state['notifications'].setdefault(key, {
                'id': key, 'room_id': rid, 'label': self.rooms[rid]['label'],
                'event': current, 'observed_utc': datetime.now(timezone.utc).isoformat(),
                'live_start': live_start,
                'status': 'pending' if self.notification_command else 'not_configured'})

    def consume(self, rid):
        row = self.row(rid)
        if not row['session']:
            return
        decision = WelcomeDecision(row['seen'], minimum=2, window=30, cooldown=300,
                                   last_sent=row['last_sent'])
        decision.pending.extend(tuple(item) for item in row['pending'])
        entries = self.store.db.execute(
            'SELECT id,received_utc,data FROM raw_events WHERE room_id=? AND id>? ORDER BY id LIMIT 1000',
            (rid, row['cursor'])).fetchall()
        for event_id, received, raw in entries:
            row['cursor'] = event_id
            try:
                items = observations(json.loads(raw), received)
                for item in items:
                    uid = item.get('sender_uid')
                    if not uid or uid in decision.seen:
                        continue
                    decision.seen.add(uid)
                    if item.get('kind') == 'entry' and row['enabled'] and row['confirmed'] == 'live':
                        decision.pending.append((datetime.fromisoformat(received).timestamp(), uid, item['author']))
            except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
                continue
        decision.pending = type(decision.pending)(e for e in decision.pending if e[0] >= time.time()-30)
        row.update(seen=list(decision.seen), pending=list(decision.pending))
        if not row['enabled'] or row['confirmed'] != 'live':
            row['pending'] = []

    def schedule_welcome(self, rid):
        row = self.row(rid)
        key = 'welcome:'+rid
        if (key in self.tasks or not row['enabled'] or row['delivery'] in ('ambiguous', 'sending')
                or not self.allowed(rid, self.accounts.epoch) or len(row['pending']) < 2
                or (row['last_sent'] is not None and time.time()-row['last_sent'] < 300)):
            return
        self.tasks[key] = asyncio.create_task(self.deliver_welcome(rid))

    async def page(self, rid):
        context = await self.browser.connect()
        pages = [p for p in context.pages if correct_page(p.url, rid)]
        if len(pages) != 1:
            raise RuntimeError('目标直播页不存在或不唯一')
        return pages[0]

    async def deliver_welcome(self, rid):
        row, cfg = self.row(rid), self.rooms[rid]
        rotation_before, variant_before = deepcopy(row.get('copy_rotation')), row.get('variant_id')
        selected = None
        reserved_rotation = None
        epoch, revision = self.accounts.epoch, row['revision']
        def valid():
            return (self.row(rid) is row and self.allowed(rid, epoch)
                    and row['enabled'] and row['revision'] == revision)
        try:
            page = await self.page(rid)
            names, limit = [e[2] for e in row['pending']], await message_limit(page)
            greeting = cfg.get('greeting', '欢迎 {names} {suffix}')
            variants = self.variants[rid]
            if variants:
                prepared = {v['id']: variant_messages(names, limit, v, greeting) for v in variants}
                if not valid():
                    return
                selected = choose_variant(variants, row.setdefault('copy_rotation', {}))
                reserved_rotation = row['copy_rotation']
                texts = prepared[selected['id']]
                row['variant_id'] = selected['id']
            else:
                texts = welcome_messages(names, limit, intro=cfg['intro'], greeting_template=greeting)
            if not valid():
                return
            row.update(delivery='sending', pending=[], acknowledgments=[], messages=texts)
            self.save()
            for index, text in enumerate(texts):
                if index:
                    await asyncio.sleep(5)
                if not valid():
                    raise RuntimeError('欢迎已暂停或直播状态变化')
                receipt = await send_chat(page, text, room_id=rid, allowed=valid)
                row['acknowledgments'].append(receipt)
                row['last_sent'] = time.time()  # 任何已确认分段都计入冷却。
                self.save()
            row.update(delivery='idle', error=None)
        except (Exception, asyncio.CancelledError) as error:
            if selected and not row.get('acknowledgments') and not getattr(error, 'send_attempted', False):
                # 确定任何一段都未发出时，不把该组算作已使用。
                targets = [row]
                current = self.row(rid)
                if current is not row and current.get('copy_rotation') is reserved_rotation:
                    targets.append(current)  # 跨场会复制房间对象，但仍共享本次预留队列。
                for target in targets:
                    if rotation_before is None:
                        target.pop('copy_rotation', None)
                    else:
                        target['copy_rotation'] = deepcopy(rotation_before)
                    if variant_before is None:
                        target.pop('variant_id', None)
                    else:
                        target['variant_id'] = variant_before
            row.update(delivery='ambiguous' if getattr(error, 'send_attempted', False) else 'paused',
                       error=type(error).__name__)
        finally:
            self.save()

    async def send_notification(self, key):
        row = self.state['notifications'][key]
        row['status'] = 'sending'
        self.save()
        event = {'live': '检测到开播', 'offline': '确认下播', 'dashboard': '实时看板'}[row['event']]
        row.setdefault('subject', f'{row["label"]} · {event}')
        payload = {**row, 'subject': row['subject'],
                   'html': f'<p>{html.escape(row["label"])}：{event}</p>'
                           f'<p>观察时间（UTC）：{row["observed_utc"]}</p>'
                            f'<p><a href="https://live.bilibili.com/{row["room_id"]}">打开直播间</a></p>'}
        if row['event'] == 'dashboard':
            payload['html'] = f'<p>{html.escape(row["label"])} · 实时看板</p><p><a href="{html.escape(row["url"], quote=True)}">打开本场看板</a></p>'
        process = None
        try:
            process = await asyncio.create_subprocess_exec(*self.notification_command,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), 90)
            row['status'] = 'accepted' if process.returncode == 0 else 'unknown'
            if process.returncode == 0:
                receipt = json.loads(output)
                row['receipt'] = receipt.get('id')
                if receipt.get('message_id'):
                    row['message_id'] = receipt['message_id']
        except (Exception, asyncio.CancelledError):
            row['status'] = 'unknown'
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            self.save()

    def start_likes(self, rid, count, request_id):
        self.row(rid)
        key = request_id
        existing = self.state['likes'].get(key)
        if existing:
            if existing['room_id'] != rid or existing['requested'] != count:
                raise ValueError('相同request_id不能复用于不同点赞请求')
            return existing
        if not self.allowed(rid, self.accounts.epoch):
            raise ValueError('当前账号未登录或房间未确认开播')
        if any(r['room_id'] == rid and r['status'] == 'running' for r in self.state['likes'].values()):
            raise ValueError('该房间已有点赞任务')
        row = {'id': key, 'room_id': rid, 'requested': count, 'triggered': 0,
               'status': 'running', 'account': self.accounts.active}
        self.state['likes'][key] = row
        self.save()
        self.tasks['like:'+key] = asyncio.create_task(self.deliver_likes(row))
        return row

    async def deliver_likes(self, row):
        epoch = self.accounts.epoch
        try:
            page = await self.page(row['room_id'])
            def progress(count):
                row['triggered'] = count
                self.save()
            await trigger_likes(page, row['room_id'], row['requested'],
                                lambda: self.allowed(row['room_id'], epoch), progress)
            row['status'] = 'completed'
        except asyncio.CancelledError:
            row['status'] = 'cancelled'
        except Exception as error:
            row.update(status='stopped', error=f'{type(error).__name__}: {error}')
        finally:
            self.save()

    async def cancel_likes(self, key):
        if key not in self.state['likes']:
            raise KeyError(key)
        task = self.tasks.get('like:'+key)
        if task and not task.done():
            self.state['likes'][key]['status'] = 'cancelled'
            self.save()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.state['likes'][key]

    async def switch_account(self, name):
        if name not in self.accounts.profiles:
            raise ValueError('未知账号profile')
        async with self.lock:
            self.accounts.switching = True
            self.accounts.epoch += 1
            self.platform.suspended = True
            self.platform.revision += 1
            self.platform.save()
            acquired = False
            try:
                await self.platform.reconcile_lock.acquire()
                acquired = True
                tasks = [t for k, t in self.tasks.items() if not k.startswith(('mail:', 'workflow:'))]
                for row in self.state['likes'].values():
                    if row['status'] == 'running':
                        row['status'] = 'cancelled'
                self.save()
                tasks += [t for t in (self.platform.status_task, self.platform.guard_task,
                                     *self.platform.launching.values()) if t]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.platform.launching.clear()
                for rid in list(self.platform.monitor.tasks):
                    await self.platform.monitor.stop(rid)
                self.platform.active.clear()
                await self.browser.close()
                self.browser.pw = self.browser.browser = self.browser.anchor = None
                await self.accounts.close()
                self.accounts.active = name
                await self.accounts.start()
                await self.accounts.refresh(self.browser)
                await self.accounts.login(self.browser)
                self.platform.next_status_check = 0
                # 不清空跨场通知去重、已见UID或欢迎冷却。
                for row in self.state['rooms'].values():
                    row['pending'] = []
                return self.summary()
            finally:
                if acquired:
                    self.platform.reconcile_lock.release()
                self.accounts.switching = False
                self.platform.suspended = False
                self.save()

    async def close(self):
        for row in self.state['likes'].values():
            if row['status'] == 'running':
                row['status'] = 'interrupted'
        tasks = [*self.tasks.values(), *([self.loop] if self.loop else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.save()
