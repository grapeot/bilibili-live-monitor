"""杂谈场最终发布通知：由既有定时同步调用，收件人沿用私有适配器。"""
import argparse
import fcntl
import hashlib
import html
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .live_share import atomic_json, epoch
from .live_share_control import verify_snapshot
from .session_runner import verify_remote


def load(path):
    return json.loads(Path(path).read_text())


def history(config):
    with sqlite3.connect(Path(config['database']).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        rows = dict(db.execute("SELECT key,data FROM control WHERE key IN ('automation','session_workflows')"))
    return load_value(rows, 'session_workflows').values(), load_value(rows, 'automation')['notifications'].values()


def load_value(rows, key):
    return json.loads(rows[key])


def final_report(config, record):
    directory = Path(record['workspace']).resolve()
    if directory.parent != Path(config['sessions_root']).resolve():
        raise ValueError('workspace_outside_authorized_root')
    local = load(directory / 'config.json')
    if (local['room_id'] != config['room_id'] or local['start_utc'] != record['start']
            or Path(local['workspace']).resolve() != directory or local.get('workflow_key') != record['key']):
        raise ValueError('session_identity_mismatch')
    if local.get('content_mode') != 'talk' or local.get('ai_enabled') is not False:
        raise ValueError('not_a_talk_session')
    state = load(directory / 'workflow.json')
    if (state.get('stage') != 'completed' or not state.get('ready') or not state.get('completed_at')
            or state.get('last_error')):
        raise ValueError('final_workflow_not_completed')
    snapshot = load(directory / 'public/snapshot.json')
    if (snapshot.get('phase') != 'completed' or snapshot.get('content_mode') != 'talk'
            or snapshot.get('session_start') != record['start']
            or epoch(snapshot['updated_at']) != state.get('cutoff')):
        raise ValueError('final_snapshot_mismatch')
    url = record['url']
    parsed, origin = urlsplit(url), urlsplit(config['public_origin'])
    if (url != local['url'] or parsed.scheme != 'https' or parsed.netloc != origin.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.startswith(config.get('public_path', '/share/live-')) or not url.endswith('/')):
        raise ValueError('report_url_mismatch')
    if not verify_snapshot(local)['valid']:
        raise ValueError('invalid_final_snapshot')
    verify_remote(local)
    return {'url': url, 'completed_at': state['completed_at'], 'mode': 'talk',
            'hashes': {name: hashlib.sha256((directory / 'public' / name).read_bytes()).hexdigest()
                       for name in ('index.html', 'snapshot.json')}}


def call_adapter(command, payload=None, extra=()):
    result = subprocess.run([*command, *extra], input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, check=True, timeout=110)
    return json.loads(result.stdout)


def notify(config, dry_run=False):
    settings = config.get('completion_notifications', {})
    if not settings.get('enabled'):
        return []
    ledger_path = Path(settings['ledger'])
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    with ledger_path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ledger = load(ledger_path) if ledger_path.exists() else {}
        records, notices = history(config)
        notices = list(notices)
        for record in records:
            if (record.get('room_id') != config['room_id'] or record.get('stage') != 'completed'
                    or epoch(record['start']) < epoch(settings['not_before'])):
                continue
            ident = 'talk-completed-' + record['key']
            previous = ledger.get(ident, {})
            if previous.get('status') in ('accepted', 'sending', 'unknown'):
                results.append({'id': ident, 'status': previous['status'], 'skipped': True})
                continue
            try:
                proof = final_report(config, record)
                parent = next((n for n in notices if n.get('room_id') == config['room_id']
                               and n.get('event') == 'offline' and n.get('live_start') == record['start']
                               and n.get('status') == 'accepted'), None)
                if not parent:
                    raise ValueError('waiting_for_offline_email')
                message_id = parent.get('message_id')
                command = settings['command']
                if not message_id and parent.get('receipt') and 'bilibili_live_monitor.broadcast_email' in command:
                    metadata = call_adapter(command, extra=('--lookup-receipt', parent['receipt']))
                    if metadata.get('id') == parent['receipt']:
                        message_id = metadata.get('message_id')
                if not isinstance(message_id, str) or not re.fullmatch(r'<[^\s<>]+@[^\s<>]+>', message_id):
                    raise ValueError('waiting_for_offline_message_id')
                name = html.escape(settings['display_name'])
                payload = {'id': ident, 'subject': 'Re: ' + parent['subject'], 'in_reply_to': message_id,
                           'html': f'<p>{name}本场直播已收尾，最终统计页面已发布。</p>'
                                   '<p>本场为杂谈模式，不进行离线转写或歌曲AI终审。</p>'
                                   f'<p><a href="{html.escape(proof["url"], quote=True)}">打开最终看板</a></p>'}
                if call_adapter(command, payload, ('--dry-run',)).get('valid') is not True:
                    raise ValueError('mail_preflight_failed')
            except Exception as error:
                results.append({'id': ident, 'status': 'waiting', 'reason': type(error).__name__ + ': ' + str(error)[:180]})
                continue
            if dry_run:
                results.append({'id': ident, 'status': 'ready', 'proof': proof})
                continue
            entry = {'status': 'sending', 'proof': proof, 'parent_receipt': parent.get('receipt'),
                     'attempted_at': datetime.now(timezone.utc).isoformat()}
            ledger[ident] = entry
            atomic_json(ledger_path, ledger)
            try:
                receipt = call_adapter(command, payload)
                if not receipt.get('id'):
                    raise ValueError('missing_provider_receipt')
                entry.update(status='accepted', receipt=receipt)
            except Exception as error:
                entry.update(status='unknown', error=type(error).__name__)
            atomic_json(ledger_path, ledger)
            results.append({'id': ident, 'status': entry['status']})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    print(json.dumps(notify(load(args.config), args.dry_run), ensure_ascii=False))


if __name__ == '__main__':
    main()
