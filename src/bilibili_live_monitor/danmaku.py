"""解码播放器已有WebSocket中的弹幕，不另建连接或使用付费接口。"""
import hashlib
import json
import struct
import time
import zlib
from collections import Counter
from datetime import datetime, timezone

import brotli
from google.protobuf.message import DecodeError

from .gift_protocol import gift_items
from .interaction_protocol import interaction_data

MAX_PACKET_BYTES = 4 * 1024 * 1024


def user_identity(uid, *, anonymous=False):
    value = str(uid) if isinstance(uid, (int, str)) and not isinstance(uid, bool) else ''
    valid = value.isdigit() and int(value) > 0 and not anonymous
    return {'sender_uid': value if valid else None,
            'identity_status': 'uid' if valid else 'unresolved'}


def guard_level(value):
    return value if type(value) is int and value in (0, 1, 2, 3) else None


def chat_message(info):
    text = info[1]
    if not isinstance(text, str) or not text.strip():
        return None
    at = event_time(info[0][4])
    user = info[2]
    medal = info[3] if len(info) > 3 and isinstance(info[3], list) else []
    identity = user_identity(user[0])
    key = hashlib.sha256(json.dumps(info, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {'text': text[:10000], 'author': str(user[1])[:200], 'at_utc': at,
            'source_key': key, **identity,
            'guard_level': guard_level(info[7]) if len(info) > 7 else None,
            'guard_source': 'danmaku_current_room',
            'medal': {'level': medal[0], 'name': medal[1], 'room_id': str(medal[3])}
            if len(medal) >= 4 else None}


def event_time(value):
    timestamp = float(value)
    if timestamp > 10**11:
        timestamp /= 1000
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def integer(value, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('事件数值必须为整数')
    number = int(value)
    if number < minimum:
        raise ValueError('事件数值越界')
    return number


def gift_message(packet):
    """只消费原子事件；COMBO_SEND等累计/广播消息不重复计入礼物。"""
    command = packet['cmd']
    data = packet['data']
    if not isinstance(data, dict):
        raise ValueError('礼物数据不是对象')
    source_time = data.get('timestamp') or data.get('start_time')
    event_at = event_time(source_time) if source_time else None
    at = event_at or datetime.now(timezone.utc).isoformat()
    user = data.get('user_info') or {}
    author = str(data.get('uname') or data.get('username') or user.get('uname') or '')[:200]
    sender = user_identity(data.get('uid'), anonymous=bool(data.get('is_mystery')))
    gift = {'event_type': command, 'sender_uid': sender['sender_uid'],
            'time_source': 'event' if event_at else 'received', 'event_at_utc': event_at}
    stable_identity = None
    if command == 'SEND_GIFT':
        gift.update(gift_id=str(data['giftId']), gift_name=str(data['giftName'])[:200],
                    num=integer(data['num'], minimum=1), coin_type=data.get('coin_type'),
                    price=integer(data['price']) if data.get('price') is not None else None,
                    total_coin=integer(data['total_coin']) if data.get('total_coin') is not None else None)
        if data.get('blind_gift'):
            gift['blind_gift'] = data['blind_gift']
        identity = data.get('tid')
        text = f"{'盲盒结果：' if gift.get('blind_gift') else '赠送 '}{gift['gift_name']} ×{gift['num']}"
        if gift['total_coin'] is not None and gift['coin_type'] in ('gold', 'silver'):
            text += f" · {'结果标价 ' if gift.get('blind_gift') else ''}{gift['total_coin']} 平台{'金币' if gift['coin_type'] == 'gold' else '银币'}"
        if gift.get('blind_gift') and gift['blind_gift'].get('original_gift_price') is not None:
            original_price = integer(gift['blind_gift']['original_gift_price'])
            text += f" · 原始盲盒价格原值 {original_price}（未换算）"
    elif command == 'GUARD_BUY':
        level = integer(data['guard_level'], minimum=1)
        guard_name = {1: '总督', 2: '提督', 3: '舰长'}.get(level, '大航海')
        gift_id = data.get('gift_id')
        gift_id = str(gift_id) if type(gift_id) in (int, str) and str(gift_id).strip() else None
        gift.update(gift_id=gift_id, gift_name=str(data.get('gift_name') or guard_name)[:200],
                    num=integer(data['num'], minimum=1), guard_level=level,
                    coin_type='gold',
                    guard_price_gold=integer(data['price']) if data.get('price') is not None else None)
        identity = data.get('order_id')
        if gift['sender_uid'] and event_at:
            stable_identity = {'sender_uid': gift['sender_uid'], 'at_utc': at,
                               'guard_level': gift['guard_level'], 'num': gift['num']}
        text = f"购买 {gift['gift_name']} ×{gift['num']}（大航海等级 {gift['guard_level']}）"
        if gift['guard_price_gold'] is not None:
            text += f" · 平台报告金额 {gift['guard_price_gold']} 金瓜子"
    else:  # SUPER_CHAT_MESSAGE，price的协议单位为人民币元，不与平台金币混加。
        gift.update(gift_name='醒目留言', num=1, price_yuan=integer(data['price']))
        identity = data.get('id')
        text = f"醒目留言 {gift['price_yuan']} 元：{str(data.get('message') or '')[:10000]}"
    identity = str(identity) if isinstance(identity, (int, str)) and str(identity) not in ('', '0') else None
    gift['event_id'] = identity
    gift['dedup_basis'] = 'event_id' if identity else 'event_fields' if stable_identity else 'payload_hash'
    fingerprint = json.dumps(stable_identity or {'at_utc': event_at, 'author': author, 'gift': gift, 'text': text},
                             ensure_ascii=False, sort_keys=True)
    key = command + ':' + (identity or hashlib.sha256(fingerprint.encode()).hexdigest())
    level = guard_level(data.get('guard_level', user.get('guard_level')))
    return {'kind': 'gift', 'gift': gift, 'text': text, 'author': author,
            'at_utc': at, 'source_key': key, **sender, 'guard_level': level,
            'guard_source': 'purchase' if command == 'GUARD_BUY' else 'gift_event'}


def packets(frame, depth=0):
    if not isinstance(frame, bytes) or len(frame) > MAX_PACKET_BYTES or depth > 3:
        return
    offset = 0
    while offset + 16 <= len(frame):
        size, header, version, operation, _ = struct.unpack_from('>IHHII', frame, offset)
        if not 16 <= header <= size or offset + size > len(frame):
            return
        payload = frame[offset + header:offset + size]
        offset += size
        if operation != 5:
            continue
        if version in (2, 3):
            decoder = zlib.decompressobj() if version == 2 else brotli.Decompressor()
            expanded = bytearray()
            for start in range(0, len(payload), 256):
                block = payload[start:start + 256]
                chunk = (decoder.decompress(block, MAX_PACKET_BYTES + 1 - len(expanded))
                         if version == 2 else decoder.process(block))
                expanded.extend(chunk)
                if len(expanded) > MAX_PACKET_BYTES or (version == 2 and decoder.unconsumed_tail):
                    raise ValueError('弹幕解压超过大小限制')
            yield from packets(bytes(expanded), depth + 1)
        elif version in (0, 1):
            try:
                yield json.loads(payload)
            except (ValueError, UnicodeError):
                continue


def entry_message(packet, *, received_utc=None):
    """只认 msg_type=1 入场；关注、分享和入场特效不重复计为到场。"""
    data = interaction_data(packet['data']) if packet['cmd'] == 'INTERACT_WORD_V2' else packet['data']
    if integer(data.get('msg_type', 0)) != 1:
        return None
    user = data.get('uinfo') or {}
    base = user.get('base') or {}
    anonymous = bool(data.get('is_mystery') or base.get('is_mystery'))
    identity = user_identity(user.get('uid') or data.get('uid'), anonymous=anonymous)
    author = '神秘人' if anonymous else str(base.get('name') or data.get('uname') or data.get('username') or '')[:200]
    if not author and not identity['sender_uid']:
        return None
    seconds = integer(data.get('timestamp') or 0)
    millis = integer(data.get('timestamp_ms') or 0)
    # 真实消息的timestamp_ms可能偏离数小时；只有与秒字段一致才用其精度。
    # 秒字段缺失时，毫秒字段还须接近本机接收时钟，否则标为接收时间。
    reference = datetime.fromisoformat(received_utc).timestamp() if received_utc else time.time()
    matching_ms = millis and (millis//1000 == seconds if seconds else abs(millis/1000-reference) <= 60)
    timestamp = millis if matching_ms else seconds
    at = event_time(timestamp) if timestamp else received_utc or datetime.now(timezone.utc).isoformat()
    fields = [identity['sender_uid'], timestamp, str(data.get('trigger_time') or ''), author]
    if anonymous or not timestamp:
        fields.append(data)
    key = 'entry:' + hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {'kind': 'entry', 'author': author or '未命名用户', 'text': '进入直播间',
            'at_utc': at, 'source_key': key, **identity,
            'guard_level': guard_level(data.get('guard_level', data.get('guard_type'))),
            'guard_source': 'interaction_event',
            'entry': {'event_type': packet['cmd'], 'time_source': 'event' if timestamp else 'received',
                      'time_field': 'timestamp_ms' if matching_ms else 'timestamp' if seconds else None}}


def messages(frame, *, include_raw=False):
    result = []
    try:
        for packet in packets(frame):
            if include_raw and isinstance(packet, dict):
                result.append({'kind': 'raw_event', 'packet': packet})
            try:
                if not isinstance(packet, dict):
                    continue
                command = packet.get('cmd', '')
                if command in ('INTERACT_WORD', 'INTERACT_WORD_V2'):
                    item = entry_message(packet)
                    if item:
                        result.append(item)
                    continue
                if command == 'SEND_GIFT_V2':
                    items = list(gift_items(packet['data']))
                    identities = Counter(str(data.get('tid')) for data in items if data.get('tid'))
                    for index, data in enumerate(items):
                        try:
                            item = gift_message({'cmd': 'SEND_GIFT', 'data': data})
                        except (ValueError, TypeError, KeyError, OverflowError, OSError, AttributeError):
                            continue  # 一个结果畸形不丢弃同批后续结果。
                        item['gift']['event_type'] = 'SEND_GIFT_V2'
                        if len(items) > 1:
                            item['gift']['batch_index'] = index
                            if not data.get('tid') or identities[str(data['tid'])] > 1:
                                item['source_key'] += f':item{index}'
                        result.append(item)
                    continue
                if command in ('SEND_GIFT', 'GUARD_BUY', 'SUPER_CHAT_MESSAGE'):
                    result.append(gift_message(packet))
                    continue
                if not str(command).startswith('DANMU_MSG'):
                    continue
                message = chat_message(packet['info'])
                if message:
                    result.append(message)
            except (ValueError, TypeError, IndexError, KeyError, OverflowError, OSError, AttributeError, DecodeError):
                continue
    except (ValueError, TypeError, IndexError, KeyError, OverflowError, OSError, zlib.error, brotli.error):
        # 畸形帧不能影响采音；已成功解码的消息仍保留。
        pass
    return result
