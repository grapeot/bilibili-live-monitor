import json
import sqlite3

from bilibili_live_monitor.live_share import iso
from bilibili_live_monitor.song_followers import add_follower_stats, net_change, render_followers


def test_net_changes_overlap_union_and_negative():
    points = [(0, 100), (70, 102), (100, 104), (190, 109), (219, 108), (249, 110), (250, 999)]
    assert net_change(points, [(70, 220)], [(0, 300)]) == 6
    assert net_change(points, [(100, 220)], [(0, 300)]) == 4
    assert net_change(points, [(70, 220), (100, 250)], [(0, 300)]) == 8  # not 6 + 6
    assert net_change([(0, 100), (30, 98)], [(0, 40)], [(0, 40)]) == -2
    assert '-2' in render_followers(-2)
    assert '0 人' in render_followers(0)


def test_separate_windows_exclude_changes_in_between():
    points = [(0, 100), (50, 102), (100, 200), (150, 199)]
    assert net_change(points, [(0, 60), (100, 160)], [(0, 160)]) == 1


def test_missing_baseline_and_collection_gaps_are_unknown():
    assert net_change([], [(70, 220)], [(0, 300)]) is None
    assert net_change([(80, 100), (190, 104)], [(70, 220)], [(0, 300)]) is None
    assert net_change([(0, 100), (190, 104)], [(70, 220)], [(50, 300)]) is None
    assert net_change([(0, 100), (150, 101), (250, 102)], [(0, 260)], [(0, 130), (150, 300)]) is None
    assert net_change([(0, 100), (30, 101)], [(0, 200)], [(0, 180)]) is None
    assert net_change([(0, 100)], [(0, 30)], []) is None
    assert '暂无数据' in render_followers(None)


def test_unchanged_counter_is_carried_only_with_continuous_raw_coverage():
    assert net_change([(0, 100)], [(300, 500)], [(0, 550)]) == 0
    assert net_change([(0, 100)], [(300, 500)], [(0, 200), (250, 550)]) is None


def test_sqlite_readonly_scope_validation_and_catalog_aggregate(tmp_path):
    database = tmp_path / 'source.db'
    base = 1893456000
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE raw_events(id INTEGER PRIMARY KEY,room_id TEXT,command TEXT,received_utc TEXT,data TEXT)')
        for room in ('1', '2'):
            for at, value in [(60, 100), (95, 102), (150, 104), (239, 106), (309, 109), (310, 999)]:
                payload = {'data': {'fans': value if room == '1' else value + 500}}
                db.execute('INSERT INTO raw_events(room_id,command,received_utc,data) VALUES (?,?,?,?)',
                           (room, 'ROOM_REAL_TIME_MESSAGE_UPDATE', iso(base + at), json.dumps(payload)))
        for value in (True, -1, '100', None):
            db.execute('INSERT INTO raw_events(room_id,command,received_utc,data) VALUES (?,?,?,?)',
                       ('1', 'ROOM_REAL_TIME_MESSAGE_UPDATE', iso(base + 149), json.dumps({'data': {'fans': value}})))
    data = {'songs': [{'performances': [{'session_id': 's', 'start': base + 90, 'end': base + 120},
                                       {'session_id': 's', 'start': base + 125, 'end': base + 190}]}]}
    original = database.read_bytes()
    sessions = [{'id': 's', 'gift_source': {'database': str(database), 'room_id': '1'}}]
    add_follower_stats(data, sessions)
    song = data['songs'][0]
    assert [p['followers_change'] for p in song['performances']] == [6, 7]
    assert song['followers_change'] == 9
    assert database.read_bytes() == original
    assert str(database) not in json.dumps(data)
    song['performances'].append({'session_id': 'missing', 'start': base + 90, 'end': base + 120})
    add_follower_stats(data, sessions)
    assert song['followers_change'] is None
