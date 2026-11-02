"""直播中/结束后只读冻结趋势，脱离浏览器和模型运行。"""
import hashlib
import json
import math
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .trends_render import render


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('时间必须包含时区偏移，例如2030-01-01T10:00:00+08:00')
    return parsed.timestamp()


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def configure_parser(commands):
    parser = commands.add_parser('trends', help='从已保存数据只读生成入场、送礼、关注和粉丝团趋势')
    actions = parser.add_subparsers(dest='action', required=True)
    build = actions.add_parser('build', help='冻结时间范围，生成独立HTML和可验证JSON')
    build.add_argument('--db', type=Path, required=True)
    build.add_argument('--room', required=True, help='规范房间号；不自动解析短号')
    build.add_argument('--since', required=True, help='含时区的开始时间，含此时刻')
    build.add_argument('--until', help='含时区的结束时间，不含此时刻；默认当前时间')
    build.add_argument('--timezone', default='Asia/Shanghai', help='显示时区，不改变统计时刻')
    build.add_argument('--bin-seconds', type=int, default=300, help='入场/送礼桶宽，60～3600秒')
    build.add_argument('--gap-seconds', type=int, default=60, help='无业务报文可疑缺口阈值，至少30秒')
    build.add_argument('--title', help='报告标题；默认房间号')
    build.add_argument('--output', type=Path, required=True, help='新的私有输出目录，不覆盖旧报告')
    verify = actions.add_parser('verify', help='不访问源库，重算冻结统计并检查HTML/JSON指纹')
    verify.add_argument('--output', type=Path, required=True)


def snapshot(dbpath, room, since, until, display_zone, width, gap_seconds, title):
    if not re.fullmatch(r'[1-9][0-9]*', room):
        raise ValueError('--room必须是规范数字房间号')
    lo, hi = instant(since), instant(until)
    ZoneInfo(display_zone)
    if not lo < hi:
        raise ValueError('--until必须晚于--since')
    if not 60 <= width <= 3600 or gap_seconds < 30:
        raise ValueError('桶宽需为60～3600秒，缺口阈值至少30秒')
    if math.ceil(hi / width) - math.floor(lo / width) > 10000:
        raise ValueError('超过10000个桶，请缩小范围或增大桶宽')
    path = dbpath.resolve(strict=True)
    # 不创建Store，不触发建表、迁移或监控控制。留一秒查询余量，再精确过滤微秒边界。
    query = 'room_id=? AND julianday({field}) BETWEEN julianday(?) AND julianday(?)'
    args = (room, stamp(lo - 1), stamp(hi + 1))
    entries, gifts, stocks, raw_times = [], [], [], []
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        for ident, received, command, payload in db.execute(
            'SELECT id,received_utc,command,CASE WHEN command=\'ROOM_REAL_TIME_MESSAGE_UPDATE\' '
            'THEN data END FROM raw_events WHERE ' + query.format(field='received_utc'), args
        ):
            t = instant(received)
            if not lo <= t < hi:
                continue
            raw_times.append({'id': ident, 't': t})
            if command == 'ROOM_REAL_TIME_MESSAGE_UPDATE':
                data = json.loads(payload).get('data') or {}
                values = {name: data.get(key) for name, key in
                          [('followers', 'fans'), ('fans_club', 'fans_club')]}
                values = {key: value if type(value) is int and value >= 0 else None
                          for key, value in values.items()}
                stocks.append({'id': ident, 't': t, **values})
        for ident, kind, source_key, payload in db.execute(
            "SELECT id,kind,source_key,data FROM timeline WHERE kind IN ('entry','gift') AND " +
            query.format(field="json_extract(data,'$.received_utc')"), args
        ):
            data = json.loads(payload)
            t = instant(data['received_utc'])
            if not lo <= t < hi:
                continue
            uid = str(data.get('sender_uid') or '')
            uid = uid if re.fullmatch(r'[1-9][0-9]*', uid) else None
            if kind == 'entry':
                entries.append({'id': ident, 't': t, 'uid': uid})
            else:
                gift = data.get('gift') or {}
                family = gift.get('event_type', 'unknown')
                if family.startswith('SEND_GIFT'):
                    family = 'SEND_GIFT'
                event = str(gift.get('event_id') or '')
                key = [family, uid, event] if event not in ('', '0') else ['source', source_key]
                gifts.append({'id': ident, 't': t, 'key': key})
    for rows in (entries, gifts, stocks, raw_times):
        rows.sort(key=lambda row: (row['t'], row['id']))
    return {'schema_version': 1, 'room_id': room, 'title': title or f'房间 {room}',
            'since': stamp(lo), 'until': stamp(hi), 'created_at': datetime.now(timezone.utc).isoformat(),
            'timezone': display_zone, 'bin_seconds': width, 'gap_seconds': gap_seconds,
            'source': {'entries': entries, 'gifts': gifts, 'stocks': stocks, 'raw_times': raw_times}}


def aggregate(data):
    lo, hi, width = instant(data['since']), instant(data['until']), data['bin_seconds']
    if not lo < hi or type(width) is not int or not 60 <= width <= 3600:
        raise ValueError('冻结范围或桶宽无效')
    count = math.ceil(hi / width) - math.floor(lo / width)
    if count > 10000:
        raise ValueError('冻结数据超过10000个桶')
    base = math.floor(lo / width) * width
    bins = [{'start': max(lo, base + i*width), 'end': min(hi, base + (i+1)*width),
             'entries': 0, 'unique_entries': 0, 'anonymous_entries': 0, 'gifts': 0, 'raw_messages': 0,
             'partial': max(lo, base + i*width) != base + i*width or hi < base + (i+1)*width}
            for i in range(count)]
    uids = [set() for _ in bins]
    all_uids, orders = set(), set()
    sources = data['source']
    for name, rows in sources.items():
        ids = set()
        for row in rows:
            if row['id'] in ids or not lo <= row['t'] < hi:
                raise ValueError(f'{name}存在重复ID或范围外记录')
            ids.add(row['id'])
    for row in sources['raw_times']:
        bins[int((row['t'] - base) // width)]['raw_messages'] += 1
    for row in sources['entries']:
        i = int((row['t'] - base) // width)
        bins[i]['entries'] += 1
        if row['uid']:
            uids[i].add(row['uid'])
            all_uids.add(row['uid'])
        else:
            bins[i]['anonymous_entries'] += 1
    for row in sources['gifts']:
        key = tuple(row['key'])
        if key not in orders:
            orders.add(key)
            bins[int((row['t'] - base) // width)]['gifts'] += 1
    for bucket, users in zip(bins, uids):
        bucket['unique_entries'] = len(users)
    times = [lo] + sorted(r['t'] for r in sources['raw_times']) + [hi]
    gaps = [{'start': a, 'end': b} for a, b in zip(times, times[1:]) if b-a > data['gap_seconds']]
    return {'bins': bins, 'gaps': gaps, 'unique_entry_uids': len(all_uids),
            'entry_events': len(sources['entries']), 'gift_events': len(orders),
            'raw_messages': len(sources['raw_times'])}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(output):
    data = json.loads((output / 'data.json').read_text())
    manifest = json.loads((output / 'manifest.json').read_text())
    if data['schema_version'] != 1 or data['summary'] != aggregate(data):
        raise ValueError('冻结统计校验失败')
    if manifest.get('files') != {name: digest(output / name) for name in ('data.json', 'index.html')}:
        raise ValueError('报告文件指纹校验失败')
    if (output / 'index.html').read_text() != render(data):
        raise ValueError('HTML与冻结数据不一致')
    return {'verified': True, 'room_id': data['room_id'], 'since': data['since'], 'until': data['until'],
            'unique_entry_uids': data['summary']['unique_entry_uids'],
            'gift_events': data['summary']['gift_events']}


def main(args):
    try:
        if args.action == 'verify':
            result = verify(args.output)
        else:
            if args.output.exists():
                raise ValueError('输出路径已存在；请使用新目录以保留冻结报告')
            data = snapshot(args.db, args.room, args.since,
                            args.until or datetime.now(timezone.utc).isoformat(),
                            args.timezone, args.bin_seconds, args.gap_seconds, args.title)
            data['summary'] = aggregate(data)
            page = render(data)
            args.output.mkdir(parents=True)
            (args.output / 'data.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
            (args.output / 'index.html').write_text(page)
            (args.output / 'manifest.json').write_text(json.dumps(
                {'schema_version': 1, 'files': {name: digest(args.output/name)
                                              for name in ('data.json', 'index.html')}}, indent=2))
            result = {**verify(args.output), 'output': str(args.output.resolve())}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        print(json.dumps({'error': f'{type(error).__name__}: {error}'}, ensure_ascii=False), file=sys.stderr)
        return 2
