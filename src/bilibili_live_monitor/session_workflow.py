"""固定监控的逐场编排入口；独立子进程承担发布、AI和离线收尾。"""
import asyncio
import hashlib
import json
import math
import re
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .live_share import atomic_json, iso
from .live_share_control import initialize
from .live_share_watch import request


def session_key(room, start):
    return hashlib.sha256(f'{room}:{start}'.encode()).hexdigest()[:24]


def validate_settings(config, rooms):
    if type(config.get('enabled', False)) is not bool:
        raise ValueError('session_workflow.enabled须为布尔值')
    if not config.get('enabled'):
        return
    if type(config.get('finalize_parallel', True)) is not bool:
        raise ValueError('finalize_parallel须为布尔值')
    if type(config.get('final_concurrency', 10)) is not int or not 1 <= config.get('final_concurrency', 10) <= 10:
        raise ValueError('final_concurrency须为1到10的整数')
    if not isinstance(config.get('final_subagent', 'general'), str) or not config.get('final_subagent', 'general').strip():
        raise ValueError('final_subagent须为非空agent名称')
    for key, default in (('max_hours', 12), ('seal_seconds', 300), ('finish_timeout', 21600)):
        value = config.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'session_workflow.{key}须为有限正数')
    if config.get('max_hours', 12) > 12:
        raise ValueError('单场最多12小时')
    if type(config.get('final_review_batches', 40)) is not int or not 1 <= config.get('final_review_batches', 40) <= 1000:
        raise ValueError('final_review_batches须为1到1000的整数')
    for key in ('workspace_root', 'opencode_python', 'opencode_cwd', 'opencode_project'):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
            raise ValueError(f'session_workflow.{key}须为绝对路径')
    if '/' not in config.get('model', ''):
        raise ValueError('session_workflow.model须包含provider/model')
    for rid in rooms:
        target = config.get('publish', {}).get('rooms', {}).get(rid) or config.get('publish', {}).get('default', {})
        if not target.get('remote', '').endswith('/') or not target.get('url', '').startswith('https://'):
            raise ValueError(f'房间{rid}缺少看板发布目标')


class SessionWorkflows:
    def __init__(self, automation, config):
        self.automation, self.config = automation, config
        validate_settings(config, automation.rooms)
        self.enabled = bool(config.get('enabled'))
        self.records = automation.store.control('session_workflows', {})
        self.next_check = 0

    def save(self):
        self.automation.store.save_control('session_workflows', self.records)

    def prepare(self, rid, candidate):
        start = datetime.strptime(candidate['live_start'], '%Y-%m-%d %H:%M:%S').replace(
            tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(ZoneInfo('UTC')).isoformat()
        key = session_key(rid, start)
        if key in self.records:
            return self.records[key]
        cfg = self.config
        directory = Path(cfg['workspace_root']) / key
        target = cfg['publish'].get('rooms', {}).get(rid) or cfg['publish']['default']
        token = 'live-' + secrets.token_urlsafe(20)
        # 保存随机URL后才创建目录/启动子进程，恢复时不会为同场更换地址。
        record = {'key': key, 'room_id': rid, 'start': start, 'workspace': str(directory),
                  'url': target['url'].rstrip('/') + '/' + token + '/',
                  'remote': target['remote'].rstrip('/') + '/' + token + '/', 'stage': 'pending'}
        self.records[key] = record
        self.save()
        return record

    def initialize(self, record):
        directory = Path(record['workspace'])
        expected = Path(self.config['workspace_root']) / record['key']
        if (directory != expected or record['key'] != session_key(record['room_id'], record['start'])):
            raise ValueError('场次目录与持久身份不匹配')
        path = directory / 'config.json'
        if path.exists() and json.loads(path.read_text()).get('workflow_key') == record['key']:
            current = json.loads(path.read_text())
            if (record.get('stage') not in ('completed', 'attention')
                    and self.automation.rooms[record['room_id']].get('content_mode', 'music') == 'talk'
                    and current.get('content_mode', 'music') != 'talk'):
                current.update(content_mode='talk', ai_enabled=False)
                atomic_json(path, current)
            return path
        cfg = self.config
        rid = record['room_id']
        settings = self.automation.platform.monitor.settings
        args = SimpleNamespace(workspace=directory, database=settings.data_dir / 'monitor.sqlite3',
            room=rid, label=self.automation.rooms[rid]['label'], start=record['start'],
            hours=cfg.get('max_hours', 12), api=cfg.get('api', 'http://127.0.0.1:8765'),
            launcher=cfg.get('launcher', 'http://127.0.0.1:7997'), ai_session=None, model=None,
            opencode_cwd=None, opencode_python=None, remote=record['remote'], url=record['url'],
            blind_gift_name=[], no_blind_gifts=False, include_guard_lamps=False)
        if not path.exists():
            initialize(args, resume=True)
        for filename, empty in (('annotations.json', {'reviews': [], 'completed_followups': [], 'song_requests': []}),
                                ('verified_songs.json', [])):
            if not (directory / filename).exists():
                atomic_json(directory / filename, empty)
        config = json.loads(path.read_text())
        config['content_mode'] = self.automation.rooms[rid].get('content_mode', 'music')
        if config['content_mode'] == 'talk':
            config.update(ai_enabled=False, talk_finalize=True)
        config.update(model=cfg['model'], opencode_python=cfg['opencode_python'],
            opencode_cwd=cfg['opencode_cwd'], opencode_project=cfg['opencode_project'],
            recording_root=str(settings.data_dir / 'recordings' / rid),
            recording_segment_seconds=settings.recording_segment_seconds,
            asr_python=settings.asr_python, asr_model=settings.asr_model,
            seal_seconds=cfg.get('seal_seconds', 300),
            final_review_batches=cfg.get('final_review_batches', 40),
            finish_timeout=cfg.get('finish_timeout', 21600), workflow_key=record['key'])
        config.update(final_timeline=True, finalize_parallel=cfg.get('finalize_parallel', True),
                      final_concurrency=cfg.get('final_concurrency', 10),
                      final_subagent=cfg.get('final_subagent', 'general'))
        audience = cfg.get('audience_crm', {}).get(rid)
        if audience:
            if not isinstance(audience, str) or not Path(audience).is_absolute():
                raise ValueError('audience_crm房间配置须指向绝对路径')
            config['audience_crm_config'] = audience
        config['followup_rules'].update(self.automation.rooms[rid].get('followup_rules', {}))
        atomic_json(path, config)
        return path

    async def recover_message_id(self, opening):
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self.automation.notification_command, '--lookup-receipt', opening['receipt'],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output, _ = await asyncio.wait_for(process.communicate(), 20)
            if process.returncode == 0:
                result = json.loads(output)
                value = result.get('message_id')
                if result.get('id') == opening['receipt'] and isinstance(value, str) and re.fullmatch(r'<[^\s<>]+@[^\s<>]+>', value):
                    opening['message_id'] = value
                    self.automation.save()
        except (OSError, ValueError, asyncio.TimeoutError):
            pass
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()

    def notify_url(self, record, state):
        if not state.get('ready'):
            return False
        notifications = self.automation.state['notifications']
        opening = next((n for n in notifications.values() if n.get('event') == 'live'
                        and n.get('room_id') == record['room_id']
                        and n.get('live_start') == record['start']), None)
        if not opening or opening.get('status') != 'accepted':
            return False
        if not opening.get('message_id'):
            command = self.automation.notification_command
            key = 'metadata:' + opening['id']
            task = self.automation.tasks.get(key)
            if ('bilibili_live_monitor.broadcast_email' in command and opening.get('receipt')
                    and (task is None or task.done()) and time.time() >= opening.get('metadata_retry_after', 0)):
                opening['metadata_retry_after'] = time.time() + 60
                self.automation.tasks[key] = asyncio.create_task(self.recover_message_id(opening))
                self.automation.save()
            return False
        ident = record['key'] + '-dashboard'
        notifications.setdefault(ident, {
            'id': ident, 'room_id': record['room_id'], 'label': self.automation.rooms[record['room_id']]['label'],
            'event': 'dashboard', 'url': record['url'], 'observed_utc': iso(time.time()),
            'status': 'pending', 'subject': 'Re: ' + opening['subject'],
            'in_reply_to': opening['message_id'], 'references': [opening['message_id']]})
        self.automation.save()
        return True

    async def tick(self):
        if not self.enabled or time.time() < self.next_check:
            return
        self.next_check = time.time() + 5
        from .automation import confirmed_state
        for rid in self.automation.rooms:
            candidate = self.automation.candidate(rid)
            if confirmed_state(candidate) == 'live' and not candidate.get('blocked') and not candidate.get('blocked_alias'):
                errors = self.automation.state.setdefault('workflow_errors', {})
                try:
                    self.prepare(rid, candidate)
                    errors.pop(rid, None)
                except (KeyError, TypeError, ValueError) as error:
                    errors[rid] = f'{type(error).__name__}: {error}'[:500]
                self.automation.save()
        jobs = None
        for record in self.records.values():
            if record['stage'] in ('completed', 'attention') and record.get('url_queued'):
                continue
            try:
                path = self.initialize(record)
                status_path = path.parent / 'workflow.json'
                state = json.loads(status_path.read_text()) if status_path.exists() else {}
                for field in ('final_session', 'finalize_progress'):
                    if field in state:
                        record[field] = state[field]
                record['url_queued'] = self.notify_url(record, state)
                if state.get('stage') in ('completed', 'attention'):
                    record['stage'] = state['stage']
                    continue
                if jobs is None:
                    jobs = await asyncio.to_thread(request, self.config.get('launcher', 'http://127.0.0.1:7997') + '/processes')
                label = 'session_monitor_' + record['key']
                if any(p.get('label') == label and p.get('status') == 'running' for p in jobs):
                    continue
                # 一个响应丢失的启动不会立刻重发；子进程还会用本场文件锁拒绝重复实例。
                if time.time() < record.get('retry_after', 0):
                    continue
                record['retry_after'] = time.time() + 60
                self.save()
                await asyncio.to_thread(request, self.config.get('launcher', 'http://127.0.0.1:7997') + '/run', {
                    'command': [sys.executable, '-m', 'bilibili_live_monitor.session_runner', '--config', str(path)],
                    'cwd': str(path.parent), 'label': label, 'diagnose_on_failure': False})
                record['stage'] = 'running'
                record.pop('error', None)
            except Exception as error:
                record['error'] = f'{type(error).__name__}: {error}'[:500]
        self.save()
