import base64
import json
import struct

import brotli
from fastapi.testclient import TestClient

from bilibili_live_monitor.app import create_app
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.danmaku import messages
from bilibili_live_monitor.models import TimelineItem
from bilibili_live_monitor.store import Store


def frame(packet):
    payload = json.dumps(packet).encode()
    return struct.pack('>IHHII', len(payload) + 16, 16, 1, 5, 1) + payload


def gift(**values):
    return {'cmd': 'SEND_GIFT', 'data': {
        'tid': 'synthetic-order-1', 'timestamp': 1792700000, 'uid': 123,
        'uname': '合成用户', 'giftId': 1, 'giftName': '合成礼物', 'num': 2,
        'price': 100, 'total_coin': 200, 'coin_type': 'gold', **values}}


def test_gift_batch_compression_currency_and_malformed_isolation():
    raw = frame(gift()) + frame(gift(num=-1)) + frame(gift(num=1, coin_type='silver', tid='free'))
    raw += frame({'cmd': 'COMBO_SEND', 'data': gift()['data']})
    raw += frame({'cmd': 'SEND_GIFT', 'data': []})
    raw += frame({'cmd': 'SEND_GIFT', 'data': {'user_info': []}})
    compressed = brotli.compress(raw)
    batch = struct.pack('>IHHII', len(compressed) + 16, 16, 3, 5, 1) + compressed
    items = messages(batch)
    assert len(items) == 2  # 两笔原子礼物，坏消息与连击累计均不计入。
    assert items[0]['gift']['total_coin'] == 200
    assert items[0]['gift']['sender_uid'] == '123'
    assert items[1]['gift']['coin_type'] == 'silver'
    assert '银币' in items[1]['text']
    for item in items:
        TimelineItem.model_validate({**item, 'id': 1, 'room_id': '123456', 'received_utc': item['at_utc']})


def test_guard_and_superchat_keep_distinct_value_units():
    guard = {'cmd': 'GUARD_BUY', 'data': {
        'start_time': 1792700000, 'uid': 456, 'username': '合成舰长', 'guard_level': 3,
        'num': 1, 'price': 198000, 'gift_name': '舰长'}}
    sc = {'cmd': 'SUPER_CHAT_MESSAGE', 'data': {
        'id': 789, 'start_time': 1792700000, 'uid': 456, 'user_info': {'uname': '合成用户'},
        'price': 30, 'message': '合成醒目留言'}}
    g, s = messages(frame(guard) + frame(sc))
    assert g['gift']['price'] == 198000 and 'price_yuan' not in g['gift']
    assert g['gift']['dedup_basis'] == 'payload_hash'
    assert s['gift']['price_yuan'] == 30 and s['gift']['event_id'] == '789'
    assert '合成醒目留言' in s['text']


def test_event_identity_dedup_restart_export_and_state_contract(tmp_path):
    store = Store(tmp_path / 'monitor.sqlite3')
    original = messages(frame(gift()))[0]
    replay = messages(frame(gift(timestamp=1792700001, uname='合成显示变更')))[0]
    another = messages(frame(gift(tid='synthetic-order-2')))[0]
    for item in (original, replay, another):
        store.live_message('123456', item)
    assert len(store.items('123456')) == 2
    assert len(store.events(0)) == 2  # 重放不产生第二条SSE事件。
    store.close()
    restored = Store(tmp_path / 'monitor.sqlite3')
    restored.live_message('123456', original)
    assert len(list(restored.export_items())) == 2
    restored.close()
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        rows = [json.loads(line) for line in client.get('/api/export').text.splitlines()]
        assert len(rows) == 2 and all(row['kind'] == 'gift' for row in rows)
        for row in rows:
            assert TimelineItem.model_validate(row).gift.total_coin == 200


def test_missing_order_id_hashes_event_fields_and_invalid_gift_does_not_drop_chat():
    original = gift(tid=None)
    assert messages(frame(original))[0]['source_key'] == messages(frame(original))[0]['source_key']
    chat = {'cmd': 'DANMU_MSG', 'info': [[0, 1, 25, 0, 1792700000000], '仍保留弹幕', [123, '合成用户']]}
    result = messages(frame(gift(timestamp='bad')) + frame(chat))
    assert len(result) == 1 and result[0]['text'] == '仍保留弹幕'


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    # 独立构造协议字节，不借用生产descriptor序列化，检查真实字段号映射。
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return varint(number * 8 + 2) + varint(len(value)) + value
    return varint(number * 8) + varint(value)


def test_v2_protobuf_batch_keeps_every_result_and_64bit_sender(tmp_path):
    item = (field(1, 10) + field(2, '合成盲盒结果') + field(3, 1) + field(5, 100)
            + field(7, 100) + field(8, 'gold') + field(9, 'same-batch-id') + field(10, 1792700000))
    sender = field(1, 2**50 + 123) + field(2, field(1, '合成新版用户'))
    blind = field(2, 9) + field(3, '合成原始盲盒') + field(6, 200)
    pb = field(1, 123) + field(2, '合成旧名') + field(9, blind) + field(10, item) * 2 + field(15, sender)
    packet = {'cmd': 'SEND_GIFT_V2', 'data': {'pb': base64.b64encode(pb).decode()}}
    decoded = messages(frame(packet))
    assert len(decoded) == 2
    assert len({x['source_key'] for x in decoded}) == 2
    assert decoded[0]['author'] == '合成新版用户'
    assert decoded[0]['gift']['sender_uid'] == str(2**50 + 123)
    assert decoded[0]['gift']['event_type'] == 'SEND_GIFT_V2'
    assert decoded[0]['gift']['blind_gift']['original_gift_price'] == 200
    store = Store(tmp_path / 'monitor.sqlite3')
    for message in decoded * 2:
        store.live_message('123456', message)
    assert len(list(store.export_items())) == 2
    for row in store.items('123456'):
        assert TimelineItem.model_validate(row).gift.num == 1
    store.close()


def test_v2_bad_base64_and_proto_do_not_discard_next_legacy_event():
    for bad in ('not base64', base64.b64encode(b'\xff').decode(), ''):
        result = messages(frame({'cmd': 'SEND_GIFT_V2', 'data': {'pb': bad}}) + frame(gift()))
        assert len(result) == 1 and result[0]['gift']['event_type'] == 'SEND_GIFT'
