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

from dotenv import load_dotenv


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
    args = parser.parse_args()
    load_dotenv(args.env_file)
    payload = json.load(sys.stdin)
    body, _ = prepare(payload, os.environ)
    if args.dry_run:
        print(json.dumps({'valid': True, 'threaded': bool(payload.get('in_reply_to'))}))
        return 0
    request = urllib.request.Request('https://api.resend.com/emails',
        data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY'],
        'Content-Type': 'application/json', 'Idempotency-Key': 'bili-' + payload['id'],
        'User-Agent': 'bilibili-live-monitor/0.1'})
    with urllib.request.urlopen(request, timeout=30) as response:
        receipt = json.load(response)
    if not receipt.get('id'):
        raise RuntimeError('通知缺少发送回执')
    # Resend可能替换自定义Message-ID；线程必须引用服务端实际采用的RFC ID。
    actual_id = None
    for attempt in range(3):
        try:
            metadata = urllib.request.Request('https://api.resend.com/emails/' + receipt['id'],
                headers={'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY'],
                         'User-Agent': 'bilibili-live-monitor/0.1'})
            with urllib.request.urlopen(metadata, timeout=10) as response:
                actual_id = json.load(response).get('message_id')
            if actual_id and re.fullmatch(r'<[^\s<>]+@[^\s<>]+>', actual_id):
                break
            actual_id = None
        except (OSError, ValueError):
            pass
        if attempt < 2:
            time.sleep(1)
    print(json.dumps({'id': receipt['id'], 'message_id': actual_id,
                      'thread_error': None if actual_id else 'provider_message_id_unavailable'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
