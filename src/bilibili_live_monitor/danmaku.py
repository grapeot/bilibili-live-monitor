"""解码播放器已有WebSocket中的弹幕，不另建连接或使用付费接口。"""
import hashlib
import json
import struct
import zlib
from datetime import datetime, timezone

import brotli
from google.protobuf.message import DecodeError

from .gift_protocol import gift_items

MAX_PACKET_BYTES = 4 * 1024 * 1024


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
    at = event_time(data.get('timestamp') or data.get('start_time'))
    user = data.get('user_info') or {}
    author = str(data.get('uname') or data.get('username') or user.get('uname') or '')[:200]
    gift = {'event_type': command, 'sender_uid': str(data['uid']) if data.get('uid') else None}
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
            text += f" · 原始盲盒单价 {original_price} 金瓜子"
    elif command == 'GUARD_BUY':
        gift.update(gift_id=str(data.get('gift_id', '')), gift_name=str(data.get('gift_name') or '大航海')[:200],
                    num=integer(data['num'], minimum=1), guard_level=integer(data['guard_level'], minimum=1),
                    coin_type='gold',
                    guard_price_gold=integer(data['price']) if data.get('price') is not None else None)
        identity = data.get('order_id')
        if gift['sender_uid']:
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
    fingerprint = json.dumps(stable_identity or {'at_utc': at, 'author': author, 'gift': gift, 'text': text},
                             ensure_ascii=False, sort_keys=True)
    key = command + ':' + (identity or hashlib.sha256(fingerprint.encode()).hexdigest())
    return {'kind': 'gift', 'gift': gift, 'text': text, 'author': author,
            'at_utc': at, 'source_key': key}


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


def messages(frame):
    result = []
    try:
        for packet in packets(frame):
            try:
                if not isinstance(packet, dict):
                    continue
                command = packet.get('cmd', '')
                if command == 'SEND_GIFT_V2':
                    items = list(gift_items(packet['data']))
                    for index, data in enumerate(items):
                        item = gift_message({'cmd': 'SEND_GIFT', 'data': data})
                        item['gift']['event_type'] = 'SEND_GIFT_V2'
                        if len(items) > 1:
                            item['gift']['batch_index'] = index
                            item['source_key'] += f':item{index}'
                        result.append(item)
                    continue
                if command in ('SEND_GIFT', 'GUARD_BUY', 'SUPER_CHAT_MESSAGE'):
                    result.append(gift_message(packet))
                    continue
                if not str(command).startswith('DANMU_MSG'):
                    continue
                info = packet['info']
                text = info[1]
                if not isinstance(text, str) or not text.strip():
                    continue
                at = event_time(info[0][4])
                key = hashlib.sha256(json.dumps(info, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                result.append({'text': text[:10000], 'author': str(info[2][1])[:200],
                               'at_utc': at, 'source_key': key})
            except (ValueError, TypeError, IndexError, KeyError, OverflowError, OSError, AttributeError, DecodeError):
                continue
    except (ValueError, TypeError, IndexError, KeyError, OverflowError, OSError, zlib.error, brotli.error):
        # 畸形帧不能影响采音；已成功解码的消息仍保留。
        pass
    return result
