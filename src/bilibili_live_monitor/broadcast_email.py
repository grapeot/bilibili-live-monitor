"""Resend通知适配器。收件人只从私有环境读取，stdin不能追加收件人。"""
import argparse
import hashlib
import json
import os
import re
import sys
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
    body, message_id = prepare(payload, os.environ)
    if args.dry_run:
        print(json.dumps({'valid': True, 'threaded': bool(payload.get('in_reply_to'))}))
        return 0
    request = urllib.request.Request('https://api.resend.com/emails',
        data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + os.environ['RESEND_API_KEY'],
        'Content-Type': 'application/json', 'Idempotency-Key': 'bili-' + payload['id']})
    with urllib.request.urlopen(request, timeout=60) as response:
        receipt = json.load(response)
    if not receipt.get('id'):
        raise RuntimeError('通知缺少发送回执')
    print(json.dumps({'id': receipt['id'], 'message_id': message_id}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
