import base64
import json
import struct

import pytest
from fastapi.testclient import TestClient

from bilibili_live_monitor.app import create_app
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.danmaku import messages
from bilibili_live_monitor.models import TimelineItem
from bilibili_live_monitor.store import Store


def frame(packet):
    data = json.dumps(packet).encode()
    return struct.pack('>IHHII', len(data)+16, 16, 1, 5, 1)+data


def varint(n):
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128)
        n >>= 7
    result.append(n)
    return bytes(result)


def field(n, value):
    if isinstance(value, int):
        return varint(n << 3)+varint(value)
    return varint((n << 3) | 2)+varint(len(value))+value


def packet(msg_type=1, timestamp=1792700000, anonymous=False):
    # 独立构造wire fixture，不用实现里的descriptor回写自身。
    uid = 2**53+123
    base = field(1, '合成访客'.encode())+field(4, int(anonymous))
    user = field(1, uid)+field(2, base)
    payload = (field(1, uid)+field(2, '合成访客'.encode())+field(5, msg_type)
               +field(7, timestamp)+field(15, timestamp*10**9)+field(16, 3)+field(22, user))
    return {'cmd': 'INTERACT_WORD_V2', 'data': {'pb': base64.b64encode(payload).decode()}}


def test_entry_v2_identity_timestamp_anonymity_and_nonentry_isolation():
    item = messages(frame(packet()))[0]
    assert item['kind'] == 'entry' and item['sender_uid'] == str(2**53+123)
    assert item['author'] == '合成访客' and item['guard_level'] == 3
    assert item['at_utc'] == '2026-10-22T20:13:20+00:00'
    assert item['entry']['time_source'] == 'event'
    anonymous = messages(frame(packet(anonymous=True)))[0]
    assert anonymous['sender_uid'] is None and anonymous['author'] == '神秘人'
    assert anonymous['identity_status'] == 'unresolved'
    assert messages(frame(packet(2))+frame(packet(3))+frame(packet(99))) == []


@pytest.mark.parametrize('bad', ['not base64', '', None, 42])
def test_malformed_entry_preserves_raw_and_following_valid_packet(bad):
    raw = frame({'cmd': 'INTERACT_WORD_V2', 'data': {'pb': bad}})+frame(packet())
    results = messages(raw, include_raw=True)
    assert sum(r['kind'] == 'raw_event' for r in results) == 2
    assert sum(r['kind'] == 'entry' for r in results) == 1


def test_legacy_entry_received_time_fallback_and_effect_not_double_counted():
    old = {'cmd': 'INTERACT_WORD', 'data': {'uid': 123, 'uname': '合成访客', 'msg_type': 1}}
    item = messages(frame(old))[0]
    assert item['entry']['time_source'] == 'received'
    assert item['source_key'] == messages(frame(old))[0]['source_key']
    effect = {'cmd': 'ENTRY_EFFECT', 'data': {'uid': 123, 'copy_writing': '合成访客来了'}}
    assert len(messages(frame(old)+frame(effect))) == 1


def test_millisecond_only_timestamp_and_distinct_anonymous_entries():
    payload = field(1, 123)+field(2, b'synthetic')+field(5, 1)+field(8, 1792700000123)
    p = {'cmd': 'INTERACT_WORD_V2', 'data': {'pb': base64.b64encode(payload).decode()}}
    item = messages(frame(p))[0]
    assert item['at_utc'] == '2026-10-22T20:13:20.123000+00:00'
    assert item['entry']['time_source'] == 'event'
    first = {'cmd': 'INTERACT_WORD', 'data': {'uid': 123, 'uname': 'synthetic1',
             'msg_type': 1, 'timestamp': 1792700000, 'is_mystery': True}}
    second = {'cmd': first['cmd'], 'data': {**first['data'], 'uid': 456, 'uname': 'synthetic2'}}
    a, b = messages(frame(first)+frame(second))
    assert a['source_key'] != b['source_key']
    assert a['sender_uid'] is b['sender_uid'] is None
    assert a['author'] == b['author'] == '神秘人'


def test_entry_persists_replay_dedup_reentry_and_api_contract(tmp_path):
    store = Store(tmp_path/'monitor.sqlite3')
    store.room({'room_id': '123456', 'url': 'https://live.bilibili.com/123456',
                'title': '合成房间', 'status': 'stopped', 'chunk_seconds': 5,
                'cover_url': '', 'error': None, 'muted': True, 'cycle_notice': False})
    first = messages(frame(packet()))[0]
    second = messages(frame(packet(timestamp=1792700030)))[0]
    for item in (first, first, second):
        store.live_message('123456', {**item, 'session_id': 'synthetic-session'})
    assert len(store.items('123456')) == 2
    assert len(store.events(0)) == 3  # 房间一次、两个不同入场。
    store.close()
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        response = client.get('/api/state')
        assert response.status_code == 200
        rows = response.json()['rooms'][0]['items']
        assert len(rows) == 2 and all(r['kind'] == 'entry' for r in rows)
        exported = [json.loads(s) for s in client.get('/api/export').text.splitlines()]
        assert len(exported) == 2
        for row in exported:
            parsed = TimelineItem.model_validate(row)
            assert parsed.sender_uid == str(2**53+123)
            assert parsed.entry['event_type'] == 'INTERACT_WORD_V2'
            assert parsed.session_id == 'synthetic-session'
