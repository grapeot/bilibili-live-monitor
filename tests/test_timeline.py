import json
import struct
import zlib

import brotli
from fastapi.testclient import TestClient

from bilibili_live_monitor.app import create_app
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.danmaku import messages
from bilibili_live_monitor.store import Store


def frame(payload, version=1, operation=5):
    return struct.pack('>IHHII', len(payload) + 16, 16, version, operation, 1) + payload


def chat(text='合成弹幕'):
    return {'cmd': 'DANMU_MSG:4:0:2:2:2:0',
            'info': [[0, 1, 25, 16777215, 1792700000000], text, [123, '合成用户']]}


def test_chat_protocol_compression_and_fragment_rejection():
    raw = frame(json.dumps(chat()).encode())
    plain = messages(raw)
    assert plain[0]['text'] == '合成弹幕'
    assert plain[0]['author'] == '合成用户'
    assert messages(frame(zlib.compress(raw + raw), 2)) == plain * 2
    assert messages(frame(brotli.compress(raw), 3)) == plain
    assert messages(raw[:-1]) == []
    assert messages(frame(b'not json')) == []
    assert messages(frame(b'bad compression', 3)) == []
    assert messages(frame(b'heartbeat', operation=3)) == []


def test_mixed_timeline_order_dedup_update_and_restart(tmp_path):
    path = tmp_path / 'monitor.sqlite3'
    store = Store(path)
    message = messages(frame(json.dumps(chat()).encode()))[0]
    store.danmaku('123456', message)
    store.danmaku('123456', message)
    segment = {'room_id': '123456', 'seq': 1, 'end_utc': '2026-10-23T01:00:00+00:00',
               'status': 'queued', 'text': '', 'error': None}
    store.segment(segment)
    store.segment({**segment, 'status': 'done', 'text': '合成转写'})
    items = store.items('123456')
    assert len(items) == 2
    assert [x['kind'] for x in items] == ['danmaku', 'transcript']
    assert items[-1]['text'] == '合成转写'
    assert len(list(store.export_items())) == 2
    assert list(store.export_items('999999')) == []
    store.close()
    restored = Store(path)
    assert restored.items('123456') == items
    restored.close()


def test_export_and_page_window_do_not_truncate_storage(tmp_path):
    store = Store(tmp_path / 'monitor.sqlite3')
    for seq in range(100):
        store.danmaku('123456', {'text': f'合成弹幕 {seq}', 'author': '测试',
                                'at_utc': '2026-10-23T01:00:00+00:00', 'source_key': str(seq)})
    assert len(store.items('123456')) == 80
    store.close()
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        response = client.get('/api/export?room_id=123456')
        records = [json.loads(line) for line in response.text.splitlines()]
        assert len(records) == 100
        assert records[0]['text'] == '合成弹幕 0'
        assert response.headers['content-type'].startswith('application/x-ndjson')
        assert client.get('/api/export?room_id=invalid').status_code == 422
