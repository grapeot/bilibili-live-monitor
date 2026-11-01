import json
import struct
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from bilibili_live_monitor.app import create_app
from bilibili_live_monitor.config import Settings
from bilibili_live_monitor.danmaku import messages
from bilibili_live_monitor.models import TimelineItem
from bilibili_live_monitor.store import Store


def frame(command, data):
    payload = json.dumps({'cmd': command, 'data': data}).encode()
    return struct.pack('>IHHII', len(payload)+16, 16, 1, 5, 1)+payload


def click(**kwargs):
    return frame('LIKE_INFO_V3_CLICK', {'uid': 2**53+123, 'uname': '合成点赞用户', **kwargs})


def test_like_identity_anonymity_and_no_invented_count():
    item = messages(click())[0]
    assert item['kind'] == 'like' and item['sender_uid'] == str(2**53+123)
    assert item['like']['time_source'] == 'received'
    assert 'num' not in item and 'count' not in item['like']
    assert item['author'] == '合成点赞用户'
    for anonymous in ({'is_mystery': True}, {'uinfo': {'base': {'name': '不显示', 'is_mystery': True}}}):
        hidden = messages(click(**anonymous))[0]
        assert hidden['sender_uid'] is None and hidden['author'] == '神秘人'


@pytest.mark.parametrize('invalid', [-1, True, 1.5, None, 'bad'])
def test_bad_total_preserves_raw_and_following_click(invalid):
    result = messages(frame('LIKE_INFO_V3_UPDATE', {'click_count': invalid})+click(), include_raw=True)
    assert sum(r['kind'] == 'raw_event' for r in result) == 2
    assert sum(r['kind'] == 'like' for r in result) == 1
    assert not any(r['kind'] == 'like_total' for r in result)


def test_latest_reported_total_is_not_summed_and_can_reset(tmp_path):
    store = Store(tmp_path/'monitor.sqlite3')
    for at, value in [('2026-01-01T10:00:00+00:00', 100),
                      ('2026-01-01T10:01:00+00:00', 120),
                      ('2026-01-01T09:00:00+00:00', 999),
                      ('2026-01-01T10:02:00+00:00', 0)]:
        item = messages(frame('LIKE_INFO_V3_UPDATE', {'click_count': value}))[0]
        store.live_message('123456', {**item, 'received_utc': at})
    assert store.like_summary('123456')['reported_total'] == 0
    assert store.like_summary('654321').get('reported_total') is None
    assert store.items('123456') == []  # 累计值不刷屏，也不冒充用户点赞。
    store.close()


def test_notifications_count_users_and_survive_state_export(tmp_path):
    store = Store(tmp_path/'monitor.sqlite3')
    store.room({'room_id': '123456', 'url': 'https://live.bilibili.com/123456',
                'title': '合成房间', 'status': 'stopped', 'chunk_seconds': 5,
                'cover_url': '', 'error': None, 'muted': True, 'cycle_notice': False})
    first = messages(click())[0]
    for item in (first, first, messages(click())[0], messages(click(is_mystery=True))[0]):
        store.live_message('123456', {**item, 'session_id': 'synthetic-session'})
    assert store.like_summary('123456') == {'notifications': 3, 'identified_users': 1}
    store.live_message('123456', messages(frame('LIKE_INFO_V3_UPDATE', {'click_count': 500}))[0])
    store.close()
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        response = client.get('/api/state')
        assert response.status_code == 200
        room = response.json()['rooms'][0]
        assert room['like_summary']['reported_total'] == 500
        assert room['like_summary']['notifications'] == 3
        assert room['like_summary']['identified_users'] == 1
        exported = [json.loads(line) for line in client.get('/api/export').text.splitlines()]
        assert len(exported) == 3
        for item in exported:
            parsed = TimelineItem.model_validate(item)
            assert parsed.kind == 'like' and parsed.session_id == 'synthetic-session'
    restored = Store(tmp_path/'monitor.sqlite3')
    assert restored.like_summary('123456')['reported_total'] == 500
    restored.close()


def test_raw_identity_allows_idempotent_replay_without_collapsing_new_notifications(tmp_path):
    store = Store(tmp_path/'monitor.sqlite3')
    for message in messages(click()+click(), include_raw=True):
        store.live_message('123456', {**message, 'session_id': 'synthetic'})
    rows = store.items('123456')
    assert len(rows) == 2
    assert len({r['raw_event_id'] for r in rows}) == 2
    assert all('_raw_token' not in r for r in rows)
    for row in rows:
        store.live_message('123456', row)
    assert store.like_summary('123456') == {'notifications': 2, 'identified_users': 1}
    assert store.db.execute('SELECT count(*) FROM raw_events').fetchone()[0] == 2
    store.close()


def test_historical_total_cannot_overwrite_concurrent_newer_total(tmp_path):
    path = tmp_path/'monitor.sqlite3'
    store = Store(path)
    store.live_message('123456', {'kind': 'like_total', 'reported_total': 100,
                                 'received_utc': '2026-01-01T10:00:00+00:00'})
    original_control = store.control
    futures = []

    def newer():
        other = Store(path)
        other.live_message('123456', {'kind': 'like_total', 'reported_total': 300,
                                     'received_utc': '2026-01-01T10:02:00+00:00'})
        other.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        def interleaving(key, default=None):
            result = original_control(key, default)
            if key.startswith('like_total') and not futures:
                futures.append(pool.submit(newer))
                if not store.db.in_transaction:
                    futures[0].result(timeout=5)
            return result
        store.control = interleaving
        store.live_message('123456', {'kind': 'like_total', 'reported_total': 200,
                                     'received_utc': '2026-01-01T10:01:00+00:00'})
        futures[0].result(timeout=5)
    store.control = original_control
    assert store.like_summary('123456')['reported_total'] == 300
    totals = [json.loads(e['data'])['summary']['reported_total'] for e in store.events(0) if e['kind'] == 'likes']
    assert totals[-1] == 300
    store.close()
