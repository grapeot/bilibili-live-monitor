"""Resend通知适配器。收件人只从私有环境读取，stdin不能追加收件人。"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv


def lookup_message_id(receipt_id):
    if not re.fullmatch(r'[a-zA-Z0-9-]+', receipt_id):
        raise ValueError('Invalid receipt ID')
    request = urllib.request.Request('https://api.resend.com/emails/' + receipt_id,
        headers={'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY'],
                 'User-Agent': 'bilibili-live-monitor/0.1'})
    with urllib.request.urlopen(request, timeout=5) as response:
        value = json.load(response).get('message_id')
    return value if isinstance(value, str) and re.fullmatch(r'<[^\s<>]+@[^\s<>]+>', value) else None


def prepare(payload, env):
    sender, to = env['BILI_NOTIFY_FROM'], env['BILI_NOTIFY_TO']
    cc = env.get('BILI_NOTIFY_CC', '')
    domain = parseaddr(sender)[1].rsplit('@', 1)[-1]
    if not re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', domain):
        raise ValueError('通知发件域名无效')
    message_id = '<bili-' + hashlib.sha256(payload['id'].encode()).hexdigest() + '@' + domain + '>'
    headers = {'Message-ID': message_id}
    if payload.get('in_reply_to'):
        parent = payload['in_reply_to']
        if not re.fullmatch(r'<[^\s<>]+@[^\s<>]+>', parent):
            raise ValueError('原始邮件Message-ID无效，不能用provider receipt代替')
        headers.update({'In-Reply-To': parent, 'References': parent})
    body = {'from': sender, 'to': [to], 'subject': payload['subject'],
            'html': payload['html'], 'headers': headers}
    if cc:
        body['cc'] = [value.strip() for value in cc.split(',') if value.strip()]
    return body, message_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--lookup-receipt')
    args = parser.parse_args()
    load_dotenv(args.env_file)
    if args.lookup_receipt:
        print(json.dumps({'id': args.lookup_receipt, 'message_id': lookup_message_id(args.lookup_receipt)}))
        return 0
    payload = json.load(sys.stdin)
    body, _ = prepare(payload, os.environ)
    if args.dry_run:
        print(json.dumps({'valid': True, 'threaded': bool(payload.get('in_reply_to'))}))
        return 0
    cache = Path(args.env_file).with_suffix('.receipts')
    cache.mkdir(mode=0o700, exist_ok=True)
    cached = cache / (hashlib.sha256(payload['id'].encode()).hexdigest() + '.json')
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    if cached.exists():
        prior = json.loads(cached.read_text())
        if prior['body_digest'] != digest:
            raise ValueError('同一通知标识不可更换邮件内容')
        receipt = prior['receipt']
    else:
        receipt = None
    request = urllib.request.Request('https://api.resend.com/emails',
        data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY'],
        'Content-Type': 'application/json', 'Idempotency-Key': 'bili-' + payload['id'],
        'User-Agent': 'bilibili-live-monitor/0.1'})
    if receipt is None:
        with urllib.request.urlopen(request, timeout=30) as response:
            receipt = json.load(response)
    if not receipt.get('id'):
        raise RuntimeError('通知缺少发送回执')
    temporary = cached.with_suffix('.tmp')
    temporary.write_text(json.dumps({'body_digest': digest, 'receipt': receipt}))
    temporary.replace(cached)
    # Resend可能替换自定义Message-ID；线程必须引用服务端实际采用的RFC ID。
    actual_id = None
    deadline = time.monotonic() + 40
    for attempt in range(8):
        try:
            actual_id = lookup_message_id(receipt['id'])
            if actual_id:
                break
            actual_id = None
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            break
        if attempt < 7:
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    print(json.dumps({'id': receipt['id'], 'message_id': actual_id,
                      'thread_error': None if actual_id else 'provider_message_id_unavailable'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
