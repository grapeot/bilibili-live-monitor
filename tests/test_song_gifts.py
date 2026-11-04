import json
import sqlite3

from bilibili_live_monitor.live_share import iso
from bilibili_live_monitor.song_gifts import add_gift_stats, render_gifts, summarize


def test_union_window_boundaries_and_distinct_people():
    events = [(69, 10, 10000, 'outside', ''), (70, 2, 2000, 'a', ''),
              (190, 3, 3000, 'a', ''), (249, 1, 0, 'b', ''), (250, 99, 99000, 'excluded', '')]
    result = summarize(events, [(70, 220), (100, 250)], [(60, 250)])
    assert result == {'gift_count': 6, 'gift_count_unknown': 0, 'gift_users': 2,
                      'gift_users_unknown_events': 0, 'gift_amount_yuan': 5,
                      'gift_amount_unknown': 0, 'window_complete': True}


def test_sqlite_backfill_deduplicates_events_and_aggregate_overlap(tmp_path):
    path = tmp_path/'source.db'
    start = 1893456000
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE timeline(id INTEGER, room_id TEXT, kind TEXT, at_utc TEXT, data TEXT);'
                         'CREATE TABLE raw_events(room_id TEXT, received_utc TEXT);')
        gift = {'sender_uid': 'a', 'gift': {'sender_uid': 'a', 'event_type': 'SEND_GIFT',
                'event_id': 'one', 'num': 2, 'coin_type': 'gold', 'total_coin': 6000}}
        for ident, room in [(1, '1'), (2, '1'), (3, '2')]:
            db.execute('INSERT INTO timeline VALUES (?,?,?,?,?)', (ident, room, 'gift', iso(start+100), json.dumps(gift)))
        db.execute('INSERT INTO raw_events VALUES (?,?)', ('1', iso(start+400)))
    data = {'songs': [{'performances': [{'session_id': 's', 'start': start+90, 'end': start+120},
                                       {'session_id': 's', 'start': start+125, 'end': start+190}]}]}
    original = path.read_bytes()
    add_gift_stats(data, [{'id': 's', 'gift_source': {'database': str(path), 'room_id': '1'}}])
    song = data['songs'][0]
    assert song['performances'][0]['gifts']['gift_count'] == 2
    assert song['performances'][1]['gifts']['gift_count'] == 2
    assert song['gifts']['gift_count'] == 2 and song['gifts']['gift_users'] == 1
    assert song['gifts']['gift_amount_yuan'] == 6
    assert path.read_bytes() == original
    assert str(path) not in json.dumps(data) and 'sender_uid' not in json.dumps(data)


def test_unknown_amount_identity_and_partial_window_are_not_fabricated_zero():
    result = summarize([(100, None, None, None, '')], [(70, 220)], [(70, 150)])
    assert result['gift_amount_unknown'] == result['gift_count_unknown'] == result['gift_users_unknown_events'] == 1
    assert not result['window_complete']
    text = render_gifts(result)
    assert '0+' in text and '未完整覆盖' in text
    assert '暂无数据' in render_gifts(None)


def test_later_observation_does_not_establish_earlier_window_coverage():
    assert not summarize([], [(70, 220)], [(500, 1000)])['window_complete']
    assert not summarize([], [(70, 220)], [(60, 100), (200, 300)])['window_complete']
