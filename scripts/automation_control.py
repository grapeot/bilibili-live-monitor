"""操作者/AI使用的动态控制CLI；不启动监控、不隐式发送点赞。"""
import argparse
import json
import uuid
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    welcome = commands.add_parser('welcome')
    welcome.add_argument('room')
    welcome.add_argument('state', choices=['on', 'off'])
    like = commands.add_parser('like')
    like.add_argument('room')
    like.add_argument('count', type=int)
    like.add_argument('--request-id', default=None, help='同一请求重试时复用该ID')
    cancel = commands.add_parser('cancel-like')
    cancel.add_argument('request_id')
    account = commands.add_parser('account')
    account.add_argument('profile')
    commands.add_parser('login')
    args = parser.parse_args()
    path, body, method = '', None, 'GET'
    if args.command == 'welcome':
        path, body, method = f'/rooms/{args.room}', {'enabled': args.state == 'on'}, 'PUT'
    elif args.command == 'like':
        path, body, method = f'/rooms/{args.room}/likes', {
            'count': args.count, 'request_id': args.request_id or str(uuid.uuid4())}, 'POST'
    elif args.command == 'cancel-like':
        path, body, method = f'/likes/{args.request_id}/stop', {}, 'POST'
    elif args.command == 'account':
        path, body, method = '/account', {'profile': args.profile}, 'POST'
    elif args.command == 'login':
        path, body, method = '/account/login', {}, 'POST'
    request = Request(args.url.rstrip('/')+'/api/automation'+path, method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=90) as response:
        print(json.dumps(json.load(response), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
