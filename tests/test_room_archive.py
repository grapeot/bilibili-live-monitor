import json
import sqlite3

import pytest

from bilibili_live_monitor.live_share import epoch, iso
from bilibili_live_monitor.room_archive import archive_data


def fixture(tmp_path):
    root = tmp_path / 'sessions'
    root.mkdir()
    db = tmp_path / 'monitor.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.executescript('''CREATE TABLE raw_events(id INTEGER PRIMARY KEY,room_id TEXT,received_utc TEXT,command TEXT,data TEXT);
        CREATE TABLE timeline(id INTEGER PRIMARY KEY,room_id TEXT,kind TEXT,at_utc TEXT,data TEXT);
        CREATE TABLE segments(room_id TEXT,seq INTEGER,data TEXT);''')
    start = epoch('2026-01-01T15:59:00+00:00')
    for key, a, b in [('a' * 24, 0, 180), ('b' * 24, 240, 300)]:
        d = root / key
        (d / 'public').mkdir(parents=True)
        (d / 'config.json').write_text(json.dumps({'room_id': '1', 'start_utc': iso(start+a)}))
        (d / 'workflow.json').write_text(json.dumps({'stage': 'live'}))
        (d / 'public/snapshot.json').write_text(json.dumps({'session_start': iso(start+a),
            'updated_at': iso(start+b), 'session_summary': {'watched': 200}}))
    with sqlite3.connect(db) as conn:
        for seq, (a, b) in enumerate([(0, 120), (90, 180), (240, 300)]):
            conn.execute('INSERT INTO segments VALUES (?,?,?)', ('1', seq,
                json.dumps({'start_utc': iso(start+a), 'end_utc': iso(start+b)})))
        for at, n in [(10, 100), (50, 102), (100, 105), (170, 106), (260, 110), (290, 109)]:
            conn.execute('INSERT INTO raw_events(room_id,received_utc,command,data) VALUES (?,?,?,?)',
                ('1', iso(start+at), 'ROOM_REAL_TIME_MESSAGE_UPDATE', json.dumps({'data': {'fans': n}})))
        for offset, event, count, amount in [(30, 'A', 2, 2000), (60, 'B', 3, 6000),
                                             (60.1, 'B', 3, 6000), (210, 'gap', 10, 10000),
                                             (270, 'C', 4, None), (310, 'future', 1, 1000)]:
            gift = {'event_type': 'SEND_GIFT_V2', 'event_id': event, 'num': count,
                    'coin_type': 'gold', 'total_coin': amount, 'sender_uid': 'PRIVATE_UID'}
            conn.execute('INSERT INTO timeline(room_id,kind,at_utc,data) VALUES (?,?,?,?)',
                ('1', 'gift', iso(start+offset), json.dumps({'gift': gift, 'source_key': str(offset)})))
        for offset, kind in [(20, 'danmaku'), (80, 'danmaku'), (90, 'entry')]:
            conn.execute('INSERT INTO timeline(room_id,kind,at_utc,data) VALUES (?,?,?,?)',
                         ('1', kind, iso(start+offset), '{}'))
    return root, db


def test_natural_day_split_dedup_gaps_and_privacy(tmp_path):
    root, db = fixture(tmp_path)
    data = archive_data(root, db, '1', '测试主播')
    days = {d['date']: d for d in data['days']}
    first, second = days['2026-01-01'], days['2026-01-02']
    assert (first['gift_count'], first['gift_amount_yuan'], first['coverage_seconds']) == (2, 2, 60)
    assert (second['gift_count'], second['gift_amount_yuan'], second['coverage_seconds']) == (7, 6, 180)
    assert second['gift_amount_unknown'] == 1
    assert (first['followers_change'], second['followers_change']) == (2, 3)
    assert second['followers_partial']
    assert len(second['session_keys']) == 2
    assert 'PRIVATE_UID' not in json.dumps(data) and 'sender_uid' not in json.dumps(data)
    assert all('watched' not in d for d in data['days'])


def test_completed_uses_offline_coverage_and_missing_fans_stay_unknown(tmp_path):
    root, db = fixture(tmp_path)
    d = root / ('a' * 24)
    (d / 'workflow.json').write_text('{"stage":"completed"}')
    (d / 'offline_segments.json').write_text(json.dumps([
        {'start_utc': '2026-01-01T15:59:30+00:00', 'end_utc': '2026-01-01T16:00:30+00:00'}]))
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM raw_events')
    data = archive_data(root, db, '1', '测试')
    assert data['days'][1]['coverage_seconds'] == 30
    assert data['days'][0]['coverage_seconds'] == 90
    assert all(d['followers_change'] is None for d in data['days'])


def test_bad_metadata_and_symlink_do_not_expose_private_files(tmp_path):
    root, db = fixture(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'config.json').write_text('{"room_id":"1","secret":"PRIVATE"}')
    (root / ('c' * 24)).symlink_to(outside, target_is_directory=True)
    data = archive_data(root, db, '1', '测试')
    assert data['warnings'] == 1
    assert len(data['sessions']) == 2
    assert 'PRIVATE' not in json.dumps(data)


def test_other_room_is_not_included(tmp_path):
    root, db = fixture(tmp_path)
    assert archive_data(root, db, '2', '别的房间')['sessions'] == []


def test_joined_late_does_not_report_unobserved_day_as_zero(tmp_path):
    root, db = fixture(tmp_path)
    d = root / ('a' * 24)
    cfg = json.loads((d / 'config.json').read_text())
    cfg['start_utc'] = '2025-12-31T15:59:00+00:00'
    (d / 'config.json').write_text(json.dumps(cfg))
    snap = json.loads((d / 'public/snapshot.json').read_text())
    snap['session_start'] = cfg['start_utc']
    (d / 'public/snapshot.json').write_text(json.dumps(snap))
    data = archive_data(root, db, '1', '测试')
    missing = next(day for day in data['days'] if day['date'] == '2025-12-31')
    assert missing['has_observations'] is False
    assert missing['gift_amount_yuan'] is None and missing['danmaku'] is None


def test_failed_asr_is_not_coverage_but_silence_is(tmp_path):
    root, db = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE segments SET data=json_set(data,'$.status','error')")
    assert all(day['coverage_seconds'] == 0 for day in archive_data(root, db, '1', '测试')['days'])
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE segments SET data=json_set(data,'$.status','empty')")
    assert sum(day['coverage_seconds'] for day in archive_data(root, db, '1', '测试')['days']) == 240


@pytest.mark.parametrize('filename', ['config.json', 'workflow.json', 'public/snapshot.json'])
def test_bad_metadata_shape_skips_only_affected_session(tmp_path, filename):
    root, db = fixture(tmp_path)
    (root / ('a' * 24) / filename).write_text('[]')
    data = archive_data(root, db, '1', '测试')
    assert data['warnings'] == 1 and len(data['sessions']) == 1
    assert data['sessions'][0]['key'] == 'b' * 24


def test_bad_offline_file_discards_partial_parse_and_falls_back(tmp_path):
    root, db = fixture(tmp_path)
    d = root / ('a' * 24)
    (d / 'workflow.json').write_text('{"stage":"completed"}')
    (d / 'offline_segments.json').write_text(json.dumps([
        {'start_utc': '2026-01-01T15:59:00+00:00', 'end_utc': '2026-01-01T16:02:00+00:00'},
        {'broken': True}]))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE segments SET data=json_set(data,'$.status','error') WHERE seq < 2")
    data = archive_data(root, db, '1', '测试')
    assert data['warnings'] == 1 and len(data['sessions']) == 2
    assert sum(day['coverage_seconds'] for day in data['days']) == 60


def test_watched_counts_use_selected_day_cutoff_and_remain_per_session(tmp_path):
    root, db = fixture(tmp_path)
    start = epoch('2026-01-01T15:59:00+00:00')
    with sqlite3.connect(db) as conn:
        for offset, count in [(30, 50), (60, 60), (150, 70), (270, 12), (310, 999)]:
            conn.execute('INSERT INTO raw_events(room_id,received_utc,command,data) VALUES (?,?,?,?)',
                ('1', iso(start+offset), 'WATCHED_CHANGE', json.dumps({'data': {'num': count}})))
    data = archive_data(root, db, '1', '测试')
    days = {d['date']: d for d in data['days']}
    assert [s['value'] for s in days['2026-01-01']['watched_sessions']] == [50]
    assert sorted(s['value'] for s in days['2026-01-02']['watched_sessions']) == [12, 70]
    assert 'watched_total' not in days['2026-01-02']


def test_missing_watched_value_is_unknown_not_zero(tmp_path):
    root, db = fixture(tmp_path)
    data = archive_data(root, db, '1', '测试')
    assert all(s['value'] is None for d in data['days'] for s in d['watched_sessions'])
