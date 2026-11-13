"""普通盲盒原始报文绑定与离线结果卡。批次以报文中的订单身份为准，不按同秒猜测。"""
import base64
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .gift_assets import canonical, download, fetch, positive, sha_file, write_json
from .gift_dynamic import BLIND_TAG, validate_dynamic
from .gift_media import absolute_time, session_config
from .gift_protocol import Gift, gift_items


def bind_event(session, event_id):
    root, config = session_config(session)
    manifest = json.loads((root/'offline/manifest.json').read_text())
    start, end = absolute_time(config['start_utc']), manifest['cutoff']
    database = Path(config['database']).resolve(strict=True)
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT id,at_utc,source_key,data FROM timeline WHERE id=? AND room_id=? AND kind=?',
                         (positive(event_id), str(config['room_id']), 'gift')).fetchone()
        if not row or not start <= absolute_time(row[1]) < end:
            raise ValueError('盲盒事件不属于所选场次')
        gift = json.loads(row[3])['gift']
        if not gift.get('blind_gift') or gift.get('event_type') != 'SEND_GIFT_V2':
            raise ValueError('盲盒须绑定SEND_GIFT_V2原始报文')
        stamp = absolute_time(row[1])
        lower, upper = [datetime.fromtimestamp(stamp+n, timezone.utc).isoformat() for n in (-2, 10)]
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_events'").fetchone():
            raise ValueError('没有盲盒原始消息，不能重建结果卡')
        raw_rows = db.execute('SELECT id,data FROM raw_events WHERE room_id=? AND command=? '
                              'AND received_utc>=? AND received_utc<? ORDER BY id LIMIT 1001',
                              (str(config['room_id']), 'SEND_GIFT_V2', lower, upper)).fetchall()
        if len(raw_rows) > 1000:
            raise ValueError('盲盒原始消息窗口过密')
        candidates = {}
        for raw_id, body in raw_rows:
            packet = json.loads(body)
            items = list(gift_items(packet['data']))
            counts = Counter(i['tid'] for i in items)
            keys = ['SEND_GIFT:'+i['tid']+(f':item{n}' if len(items)>1 and counts[i['tid']]>1 else '')
                    for n,i in enumerate(items)]
            if row[2] not in keys:
                continue
            if any(not i['tid'] for i in items):
                raise ValueError('缺少订单身份，不能推断盲盒批次')
            digest = hashlib.sha256(canonical(packet)).hexdigest()
            candidates.setdefault(digest, (raw_id, packet, items, keys))
        if len(candidates) != 1:
            raise ValueError('不能唯一绑定盲盒原始报文')
        raw_id, packet, items, keys = next(iter(candidates.values()))
        proto = Gift.FromString(base64.b64decode(packet['data']['pb'], validate=True))
        if not proto.HasField('blind_gift') or len(set(keys)) != len(keys) or not 1 <= len(items) <= 10:
            raise ValueError('盲盒结果结构或数量暂不支持')
        box_id = positive(proto.blind_gift.original_gift_id)
        timelines, results = [], []
        for index, (item, key) in enumerate(zip(items, keys, strict=True)):
            matches = db.execute('SELECT id,at_utc,source_key,data FROM timeline WHERE room_id=? AND kind=? '
                                 'AND source_key=? ORDER BY id', (str(config['room_id']), 'gift', key)).fetchall()
            matches = [m for m in matches if start <= absolute_time(m[1]) < end]
            if len(matches) != 1:
                raise ValueError('盲盒结果须在本场时间线唯一且完整，不接受缺项或重复')
            saved = matches[0]
            value = json.loads(saved[3])['gift']
            if (str(value.get('gift_id')) != str(item['giftId']) or value.get('num') != item['num']
                    or absolute_time(saved[1]) != item['timestamp'] or item['timestamp'] != stamp
                    or str(value.get('sender_uid')) != str(gift.get('sender_uid'))
                    or str(value.get('sender_uid')) != str(item['uid'] or None)
                    or str((value.get('blind_gift') or {}).get('original_gift_id')) != str(box_id)):
                raise ValueError('盲盒结果字段与原始批次冲突')
            snapshot = proto.items[index].gift_info
            if not proto.items[index].HasField('gift_info') or not snapshot.img_basic:
                raise ValueError('盲盒结果缺少原始素材快照')
            if snapshot.effect_id:
                raise ValueError('此盲盒结果另有独立动画，当前不能完整重建')
            timelines.append(saved)
            results.append({'gift_id': positive(item['giftId']), 'num': positive(item['num']),
                            'name': item['giftName'], 'image_url': snapshot.img_basic, 'effect_id': 0})
    return {'session': str(root), 'room_id': positive(config['room_id']), 'gift_id': box_id,
            'count': 1, 'event_id': min(t[0] for t in timelines), 'event_ids': sorted(t[0] for t in timelines),
            'at': row[1], 'raw_event_id': raw_id, 'results': results,
            'evidence_sha256': hashlib.sha256(canonical({'raw': packet, 'timeline': timelines})).hexdigest()}


def resource_index(cache, binding):
    return Path(cache)/'blind-resources'/f'{binding["evidence_sha256"]}.json'


def store_image(cache, body):
    import io
    with Image.open(io.BytesIO(body)) as image:
        if image.format != 'PNG' or max(image.size) > 4096 or min(image.size) < 1:
            raise ValueError('结果卡仅支持不超过4096像素的PNG图片')
        image.verify()
    digest = hashlib.sha256(body).hexdigest()
    path = Path(cache).resolve()/'objects'/f'{digest}.png'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(body)
    if sha_file(path) != digest:
        raise ValueError('结果卡缓存对象损坏')
    return {'path': str(path), 'sha256': digest}


def fetch_image(cache, url, refresh=False):
    index = Path(cache)/'blind-images'/f'{hashlib.sha256(url.encode()).hexdigest()}.json'
    if index.exists() and not refresh:
        result = json.loads(index.read_text())
        if result['url'] != url or sha_file(result['path']) != result['sha256']:
            raise ValueError('盲盒图片缓存索引损坏')
        return result
    result = {**store_image(cache, download(url, 4*1024**2)), 'url': url}
    write_json(index, result)
    return result


def fetch_event(cache, session, event_id, *, catalog=None, cdp_url=None, refresh=False,
                card_background=None, card_digits=None):
    binding = bind_event(session, event_id)
    asset_result = fetch(cache, binding['room_id'], binding['gift_id'], 1,
                         catalog=catalog, cdp_url=cdp_url, refresh=refresh)
    index = resource_index(cache, binding)
    if not index.exists() or refresh:
        if not card_background or not card_digits:
            raise ValueError('盲盒卡片须显式提供--card-background PNG与--card-digits目录（0.png至9.png）')
        resources = {'binding': binding,
                     'background': store_image(cache, Path(card_background).read_bytes()),
                     'digits': [store_image(cache, (Path(card_digits)/f'{n}.png').read_bytes()) for n in range(10)],
                     'icons': [fetch_image(cache, i['image_url'], refresh) for i in binding['results']]}
        write_json(index, resources)
    resources = load_resources(cache, binding)
    return {**asset_result, 'event': binding, 'resources': str(index),
            'resources_sha256': hashlib.sha256(canonical(resources)).hexdigest()}


def load_resources(cache, binding):
    resources = json.loads(resource_index(cache, binding).read_text())
    if resources['binding'] != binding:
        raise ValueError('盲盒卡片资源与批次绑定不符')
    validate_resources(resources)
    return resources


def validate_resources(resources):
    binding = resources['binding']
    if bind_event(binding['session'], binding['event_id']) != binding:
        raise ValueError('盲盒原始消息或结果绑定已改变')
    if len(resources['icons']) != len(binding['results']) or len(resources['digits']) != 10:
        raise ValueError('结果卡图标或数字资源缺失')
    for item in [resources['background'], *resources['digits'], *resources['icons']]:
        if sha_file(item['path']) != item['sha256']:
            raise ValueError('结果卡图片指纹不符')


def card_images(config, resources):
    validate_resources(resources)
    sources = validate_dynamic(config)
    if len(sources) != 1 or sources[0]['srcTag'] != BLIND_TAG:
        raise ValueError('普通盲盒需要唯一的已知结果图片槽')
    results = resources['binding']['results']
    count = len(results)
    if not 1 <= count <= 10:
        raise ValueError('结果卡仅支持1至10个结果条目')
    canvas = Image.new('RGBA', (644, 290))
    size, icon, inset = ((253,142,55) if count==1 else (220,124,50) if count==2
                         else (195,110,45) if count==3 else (140,82,30) if count<=8 else (124,72,25))
    rows = [count] if count<=4 else [(count+1)//2, count//2]
    bg = Image.open(resources['background']['path']).convert('RGBA').resize((size,size), Image.Resampling.LANCZOS)
    index = 0
    for r, columns in enumerate(rows):
        y = 145-size*len(rows)/2+r*size
        for column in range(columns):
            x = 322-size*columns/2+column*size
            canvas.alpha_composite(bg, (round(x),round(y)))
            picture = Image.open(resources['icons'][index]['path']).convert('RGBA')
            canvas.alpha_composite(picture.resize((icon,icon), Image.Resampling.LANCZOS),
                                   (round(x+inset),round(y+inset)))
            number = results[index]['num']
            if not (count==1 and number==1):
                height = .2*size
                width = height/1.5
                for place, digit in enumerate(str(number)[::-1]):
                    glyph = Image.open(resources['digits'][int(digit)]['path']).convert('RGBA')
                    canvas.alpha_composite(glyph.resize((round(width),round(height)), Image.Resampling.LANCZOS),
                        (round(x+size-.225*size-width*(place+1)+4), round(y+size-.15*size-height)))
            index += 1
    return {sources[0]['srcId']: canvas}
