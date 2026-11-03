"""单场可恢复工作流：实时发布→停止AI→封口→离线ASR→最终静态页。"""
import argparse
import fcntl
import hashlib
import json
import re
import shlex
import subprocess
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from . import live_share_watch as watch
from .live_share import atomic_json, fingerprint, iso
from .live_share_control import (
    export_snapshot,
    load_config,
    read_session_source,
    source_at_cutoff,
    verify_snapshot,
)
from .session_ai import ai_control
from .session_offline import recording_files, run_offline


def _strip_cdn_beacon(match):
    class Script(HTMLParser):
        sources = ()

        def handle_starttag(self, tag, attrs):
            self.sources = tuple(value for key, value in attrs if tag == 'script' and key == 'src')

    parser = Script()
    parser.feed(match.group().decode())
    if (len(parser.sources) == 1 and parser.sources[0]
            and re.fullmatch(r'https://static\.cloudflareinsights\.com/beacon\.min\.js(?:[/?][^\s]*)?', parser.sources[0])):
        return b''
    return match.group()


def verify_remote(config):
    for name in ('index.html', 'snapshot.json'):
        local = (Path(config['workspace']) / 'public' / name).read_bytes()
        req = urllib.request.Request(config['url'] + name,
                                     headers={'Cache-Control': 'no-cache', 'User-Agent': 'bilibili-live-monitor/0.1'})
        with urllib.request.urlopen(req, timeout=20) as response:
            remote = response.read()
        if name == 'index.html':
            remote = re.sub(rb'<script\b[^>]*></script>\n?', _strip_cdn_beacon, remote)
        if hashlib.sha256(remote).digest() != hashlib.sha256(local).digest():
            raise RuntimeError('远端看板内容与本次本地产物不一致')


def stop_ai(config, session, *, children=False):
    if not session:
        return True
    children_stopped = True
    if children:
        for child in ai_control(config, 'children', session=session):
            children_stopped = stop_ai(config, child, children=True) and children_stopped
    if ai_control(config, 'status', session=session).get('type') in ('busy', 'retry'):
        ai_control(config, 'abort', session=session)
        return False
    return children_stopped


def ensure_ai(config, purpose):
    title = f'{purpose} {config["label"]} {config["workflow_key"]}'
    return ai_control(config, 'create', project=config['opencode_project'], title=title)['id']


def verify_local(config):
    report = verify_snapshot(config)
    if not report['valid']:
        raise RuntimeError('本地看板验收失败：' + ','.join(report['errors']))
    return report


def advance(config, state, now):
    workspace = Path(config['workspace'])
    config_path = workspace / 'config.json'
    phase = state.setdefault('stage', 'created')
    if phase == 'created':
        if not state.get('ai_preflight'):
            ai_control(config, 'preflight', model=config['model'])
            state['ai_preflight'] = True
        if not state.get('remote_prepared'):
            host, directory = config['remote'].split(':', 1)
            subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host,
                            'mkdir -p -- ' + shlex.quote(directory)],
                           check=True, capture_output=True, timeout=30)
            state['remote_prepared'] = True
        if not config.get('ai_session'):
            config.update(ai_session=ensure_ai(config, 'update_periodic'), ai_enabled=True)
            atomic_json(config_path, {k: v for k, v in config.items() if not k.startswith('_')})
        state['stage'] = 'live'
        return
    if phase == 'live':
        live_state = watch.cycle(config, workspace / 'state.json', 'once')
        if live_state.get('phase') == 'completed':
            state.update(stage='stopping', cutoff=live_state['cutoff'],
                         finish_deadline=now + config.get('finish_timeout', 21600))
            return
        if live_state.get('last_error'):
            raise RuntimeError(live_state['last_error'])
        verify_local(config)
        if not state.get('ready'):
            verify_remote(config)
            state['ready'] = True
        slot = int(now // 300)
        if state.get('ai_slot') != slot:
            state['ai_slot'] = slot
            # 提交之前落盘；响应丢失不重复派发同一轮。
            atomic_json(workspace / 'workflow.json', state)
            _, _, source = source_at_cutoff(config)
            watch.ai_once(config, live_state, source, now)
        return
    if phase == 'stopping':
        if not stop_ai(config, config.get('ai_session')):
            return
        state.update(stage='sealing', stable_since=now)
        return
    if phase == 'sealing':
        groups = recording_files(config, state['cutoff'])
        signature = sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                           for files in groups.values() for _, p in files)
        # JSON往返后tuple变list，保持同一表示以便恢复。
        signature = [list(item) for item in signature]
        if state.get('media_signature') != signature:
            state.update(media_signature=signature, stable_since=now)
            return
        platform = watch.request(config['api'] + '/api/platform')
        candidate = next((c for c in platform['candidates'] if c.get('room_id') == config['room_id']), {})
        source = next((s for s in platform['sources'] if s.get('room_id') == config['room_id']), {})
        same_live = candidate.get('live_status') == 1 and candidate.get('live_start') == config['live_start']
        if same_live or (source.get('status') != 'stopped' and candidate.get('live_start') == config['live_start']):
            return
        if now - state['stable_since'] >= config.get('seal_seconds', 300):
            state['stage'] = 'offline_asr'
        return
    if phase == 'offline_asr':
        config['offline_segments'] = run_offline(config, state)
        atomic_json(config_path, {k: v for k, v in config.items() if not k.startswith('_')})
        state['stage'] = 'offline_review'
        return
    if phase == 'offline_review':
        if not state.get('final_session'):
            state['final_session'] = ensure_ai(config, 'finalize')
            return
        if config.get('finalize_parallel'):
            from . import session_finalize
            if not state.get('finalize_submitted'):
                plan = session_finalize.prepare(config)
                if state.get('finalize_sending'):
                    if not ai_control(config, 'submitted', session=state['final_session'], marker='FINALIZE_PLAN:' + plan['id']):
                        raise RuntimeError('总任务发送回执未确认，保留现场且不重复提交')
                else:
                    def handoff():
                        state['finalize_sending'] = True
                        atomic_json(workspace / 'workflow.json', state)
                    try:
                        session_finalize.submit(config, state['final_session'], on_handoff=handoff)
                    except session_finalize.SubmissionNotSent:
                        state.pop('finalize_sending', None)
                        atomic_json(workspace / 'workflow.json', state)
                        raise
                state.update(finalize_submitted=True, finalize_sending=False)
                atomic_json(workspace / 'workflow.json', state)
            state['finalize_progress'] = session_finalize.progress(config)
            if session_finalize.complete(config):
                session_finalize.finish(config)
                state['stage'] = 'finalizing'
            return
        if ai_control(config, 'status', session=state['final_session']).get('type') in ('busy', 'retry'):
            return
        export_snapshot(config)
        report = verify_local(config)
        source = read_session_source(config)
        reviews = json.loads((workspace / 'annotations.json').read_text()).get('reviews', [])
        reviewed = {r.get('seq'): r.get('fingerprint') for r in reviews
                    if isinstance(r, dict) and r.get('investigation_version') == 2
                    and r.get('label') in ('singing', 'speaking')
                    and r.get('music_source') in ('probable_live', 'recording', 'uncertain', 'none')}
        remaining = sum(s.get('is_final') and bool(s.get('text')) and reviewed.get(s['seq']) != fingerprint(s)
                        for s in source['segments'])
        state.update(unreviewed_windows=remaining, unpublished_songs=report['unpublished_songs'])
        if not remaining and not report['unpublished_songs']:
            state['stage'] = 'finalizing'
            return
        if state.get('review_batches', 0) >= config.get('final_review_batches', 40):
            raise ValueError('离线复核已到批次上限，尚有未审窗口')
        state['review_batches'] = state.get('review_batches', 0) + 1
        atomic_json(workspace / 'workflow.json', state)
        final_config = {**config, 'ai_session': state['final_session']}
        watch.ai_once(final_config, {'cutoff': state['cutoff']}, source, now, final=True)
        return
    if phase == 'finalizing':
        if not stop_ai(config, state.get('final_session'), children=config.get('finalize_parallel', False)):
            return
        if config.get('finalize_parallel'):
            from .session_finalize import finish
            finish(config)  # 所有写入者停止后，重验实际待发布内容。
        export_snapshot(config)
        verify_local(config)
        watch.publish(config, workspace / 'public')
        verify_remote(config)
        state.update(stage='completed', completed_at=iso(now), last_error=None)


def run(path):
    config = load_config(path)
    workspace = Path(config['workspace'])
    with (workspace / 'workflow.lock').open('a') as lock, (workspace / 'publisher.lock').open('a') as publisher:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(publisher, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status_path = workspace / 'workflow.json'
        state = json.loads(status_path.read_text()) if status_path.exists() else {'stage': 'created'}
        while state['stage'] not in ('completed', 'attention'):
            started = time.time()
            config = load_config(path)
            try:
                if started > state.get('finish_deadline', float('inf')):
                    raise ValueError('离线收尾超过本场预算')
                advance(config, state, started)
                state['last_error'] = None
                state['consecutive_errors'] = 0
            except Exception as error:
                state['last_error'] = f'{type(error).__name__}: {error}'[:500]
                state['consecutive_errors'] = state.get('consecutive_errors', 0) + 1
                if isinstance(error, ValueError) or state['consecutive_errors'] >= 10:
                    # 收尾失败亦先停本场专属AI，不重新打开实时调度。
                    stopped = True
                    for session in (config.get('ai_session'), state.get('final_session')):
                        if session:
                            try:
                                stopped = stop_ai(config, session, children=config.get('finalize_parallel', False)) and stopped
                            except Exception:
                                stopped = False
                    if stopped:
                        state['stage'] = 'attention'
            state['updated_at'] = iso(time.time())
            atomic_json(status_path, state)
            if state['stage'] not in ('completed', 'attention'):
                time.sleep(max(1, (60 if state['stage'] == 'live' or state.get('last_error') else 10) - (time.time() - started)))
        return 0 if state['stage'] == 'completed' else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    return run(parser.parse_args().config.resolve())


if __name__ == '__main__':
    raise SystemExit(main())
