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


def test_private_multi_uid_share_is_opt_in_and_never_serialized(tmp_path):
    from datetime import datetime
    root, db = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE timeline SET data=replace(data,'PRIVATE_UID','999001') WHERE kind='gift'")
        conn.execute("UPDATE timeline SET data=replace(data,'999001','999002') WHERE kind='gift' AND data LIKE '%\"B\"%'")
    now = datetime.fromisoformat('2026-01-01T16:30:00+00:00')
    config = {'enabled': True, 'label': '测试观众', 'uids': ['999001', '999002']}
    data = archive_data(root, db, '1', '测试', gift_share=config, now=now)
    assert data['periods'][0]['gift_amount_yuan'] == 8
    assert data['periods'][0]['entries_daily_mean'] == .5
    assert data['gift_share']['percentage'] is None  # unknown price in second session
    assert '999001' not in json.dumps(data) and '999002' not in json.dumps(data)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM timeline WHERE data LIKE '%\"C\"%'")
    data = archive_data(root, db, '1', '测试', gift_share=config, now=now)
    assert data['periods'][0]['gift_share']['percentage'] == 100
    assert 'gift_share' not in archive_data(root, db, '1', '测试', gift_share={**config, 'enabled': False})


def test_historical_windows_include_legacy_day_without_inventing_sessions(tmp_path):
    from datetime import datetime
    root, db = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO timeline(room_id,kind,at_utc,data) VALUES (?,?,?,?)',
                     ('1', 'entry', '2025-12-31T02:00:00+00:00', '{}'))
        conn.execute('INSERT INTO timeline(room_id,kind,at_utc,data) VALUES (?,?,?,?)',
                     ('1', 'gift', '2025-12-31T02:00:00+00:00', json.dumps({'gift': {
                         'event_type': 'SEND_GIFT', 'num': 1, 'coin_type': 'gold', 'total_coin': 3000}})))
    windows = [{'start':'2025-12-31T01:00:00+00:00', 'end':'2025-12-31T03:00:00+00:00'}]
    data = archive_data(root, db, '1', '测试', historical_windows=windows,
                        now=datetime.fromisoformat('2026-01-02T00:30:00+08:00'))
    old = next(day for day in data['days'] if day['date'] == '2025-12-31')
    assert old['entries'] == 1 and old['gift_amount_yuan'] == 3
    assert old['session_keys'] == [] and len(data['sessions']) == 2
    assert data['periods'][0]['observed_days'] == 3


def test_zero_duration_session_remains_readable(tmp_path):
    root, db = fixture(tmp_path)
    for directory in root.iterdir():
        snapshot_path = directory/'public/snapshot.json'
        snapshot = json.loads(snapshot_path.read_text())
        snapshot['updated_at'] = snapshot['session_start']
        snapshot_path.write_text(json.dumps(snapshot))
    assert len(archive_data(root, db, '1', '测试')['sessions']) == 2


def test_legacy_entries_recover_missing_timeline_without_duplicates(tmp_path):
    from bilibili_live_monitor.danmaku import entry_message
    root, db = fixture(tmp_path)
    stamp = '2025-12-31T02:00:00+00:00'
    packet = {'cmd':'INTERACT_WORD','data':{'msg_type':1,'uid':999003,'uname':'测试'}}
    existing = entry_message(packet, received_utc=stamp)
    second = {'cmd':'INTERACT_WORD','data':{'msg_type':1,'uid':999004,'uname':'测试二'}}
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO timeline(room_id,kind,at_utc,data) VALUES (?,?,?,?)',
                     ('1', 'entry', stamp, json.dumps(existing)))
        for event in [packet, second, second]:
            conn.execute('INSERT INTO raw_events(room_id,received_utc,command,data) VALUES (?,?,?,?)',
                         ('1', stamp, 'INTERACT_WORD', json.dumps(event)))
    data = archive_data(root, db, '1', '测试', historical_windows=[{
        'start':'2025-12-31T01:00:00+00:00','end':'2025-12-31T03:00:00+00:00'}])
    assert next(day for day in data['days'] if day['date']=='2025-12-31')['entries'] == 2
    assert '999003' not in json.dumps(data) and '999004' not in json.dumps(data)


def test_read_only_history_is_included_in_summary_and_days(tmp_path):
    root, db = fixture(tmp_path)
    history = tmp_path/'history'
    history.mkdir()
    (root/('a'*24)).rename(history/('a'*24))
    data = archive_data(root, db, '1', '测试', historical_root=history)
    assert len(data['sessions'])==2 and len(data['days'])==2
    assert any(row['key']=='a'*24 and row['has_result'] for row in data['sessions'])
    assert str(history) not in json.dumps(data)


def test_nickname_analysis_uses_real_uid_for_opt_in_share(tmp_path):
    from datetime import datetime
    root, db = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM timeline WHERE data LIKE '%\"C\"%'")
        rows = conn.execute("SELECT id,data FROM timeline WHERE kind='gift'").fetchall()
        for ident, raw in rows:
            value = json.loads(raw)
            value.update(analysis_identity_basis='nickname', room_id='1', author='合成昵称')
            value['gift']['sender_uid'] = '999001'
            conn.execute('UPDATE timeline SET data=? WHERE id=?',(json.dumps(value),ident))
    config = {'enabled':True,'label':'测试','uids':['999001','999002']}
    kwargs = {'gift_share':config,'now':datetime.fromisoformat('2026-01-02T01:00:00+08:00')}
    data = archive_data(root,db,'1','测试',**kwargs)
    assert data['periods'][0]['gift_share']['percentage']==100
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE timeline SET data=json_set(data,'$.sender_hidden',json('true')) WHERE kind='gift'")
    data = archive_data(root,db,'1','测试',**kwargs)
    assert data['periods'][0]['gift_share']['percentage'] is None
    assert '999001' not in json.dumps(data) and '合成昵称' not in json.dumps(data)


def test_asr_only_days_do_not_become_zero_message_days(tmp_path):
    from datetime import datetime
    root, db = fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM timeline')
        conn.execute('DELETE FROM raw_events')
    kwargs = {'now':datetime.fromisoformat('2026-01-02T01:00:00+08:00')}
    data = archive_data(root,db,'1','测试',**kwargs)
    assert data['periods'][0]['observed_days']==0
    assert data['periods'][0]['entries_daily_mean'] is None
    assert data['periods'][0]['gift_amount_yuan'] is None
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO timeline(room_id,kind,at_utc,data) VALUES (?,?,?,?)',
                     ('1','entry','2026-01-01T16:00:30+00:00','{}'))
    data = archive_data(root,db,'1','测试',**kwargs)
    assert data['periods'][0]['observed_days']==1
    assert data['periods'][0]['entries_daily_mean']==1
    assert data['periods'][0]['gift_amount_yuan']==0


def test_fans_club_daily_close_and_change_cross_midnight(tmp_path):
    from datetime import datetime
    root, db = fixture(tmp_path)
    start = epoch('2026-01-01T15:59:00+00:00')
    with sqlite3.connect(db) as conn:
        # 第一天两次推送，第二天两次推送；两场之间空档与场次外的推送不计入。
        for at, n in [(10, 40), (50, 42), (100, 39), (210, 55), (290, 37), (1000, 99)]:
            conn.execute('INSERT INTO raw_events(room_id,received_utc,command,data) VALUES (?,?,?,?)',
                ('1', iso(start+at), 'ROOM_REAL_TIME_MESSAGE_UPDATE',
                 json.dumps({'data': {'fans': 1, 'fans_club': n}})))
    data = archive_data(root, db, '1', '测试', now=datetime.fromisoformat('2026-01-02T12:00:00+08:00'))
    days = {d['date']: d for d in data['days']}
    first, second = days['2026-01-01'], days['2026-01-02']
    assert (first['fans_club_close'], first['fans_club_change'], first['fans_club_change_since']) == (42, None, None)
    assert first['fans_club_close_at'] == iso(start+50)
    assert (second['fans_club_close'], second['fans_club_change'], second['fans_club_change_since']) == (37, -5, '2026-01-01')
    week = data['periods'][0]
    assert (week['fans_club_latest'], week['fans_club_latest_date'], week['fans_club_daily_mean']) == (37, '2026-01-02', 39.5)
    assert (week['fans_club_change'], week['fans_club_change_since']) == (-5, '2026-01-01')


def test_missing_fans_club_stays_unknown(tmp_path):
    from datetime import datetime
    root, db = fixture(tmp_path)
    data = archive_data(root, db, '1', '测试', now=datetime.fromisoformat('2026-01-02T12:00:00+08:00'))
    assert all(d['fans_club_close'] is None and d['fans_club_change'] is None for d in data['days'])
    assert data['periods'][0]['fans_club_daily_mean'] is None


def test_fans_club_day_without_push_is_skipped_as_baseline(tmp_path):
    from datetime import datetime
    root, db = fixture(tmp_path)
    for key, day in [('c' * 24, '2026-01-03'), ('d' * 24, '2026-01-04')]:
        d = root / key
        (d / 'public').mkdir(parents=True)
        (d / 'config.json').write_text(json.dumps({'room_id': '1', 'start_utc': day + 'T04:00:00+00:00'}))
        (d / 'workflow.json').write_text(json.dumps({'stage': 'completed'}))
        (d / 'public/snapshot.json').write_text(json.dumps({'session_start': day + 'T04:00:00+00:00',
            'updated_at': day + 'T05:00:00+00:00', 'session_summary': {}}))
    with sqlite3.connect(db) as conn:
        for at, n in [('2026-01-01T16:03:50+00:00', 30), ('2026-01-04T04:30:00+00:00', 25)]:
            conn.execute('INSERT INTO raw_events(room_id,received_utc,command,data) VALUES (?,?,?,?)',
                ('1', at, 'ROOM_REAL_TIME_MESSAGE_UPDATE', json.dumps({'data': {'fans': 1, 'fans_club': n}})))
    data = archive_data(root, db, '1', '测试', now=datetime.fromisoformat('2026-01-04T12:00:00+08:00'))
    days = {d['date']: d for d in data['days']}
    assert (days['2026-01-03']['fans_club_close'], days['2026-01-03']['fans_club_change']) == (None, None)
    assert (days['2026-01-04']['fans_club_change'], days['2026-01-04']['fans_club_change_since']) == (-5, '2026-01-02')


def test_handoff_history_merges_two_databases_without_boundary_double_count(tmp_path):
    from datetime import datetime
    tables = '''CREATE TABLE raw_events(id INTEGER PRIMARY KEY,room_id TEXT,received_utc TEXT,command TEXT,data TEXT);
        CREATE TABLE timeline(id INTEGER PRIMARY KEY,room_id TEXT,kind TEXT,at_utc TEXT,data TEXT);
        CREATE TABLE segments(room_id TEXT,seq INTEGER,data TEXT);'''
    cutoff = '2026-01-02T04:00:00+00:00'
    plan = {'old': [('c' * 24, '2026-01-01T04:00:00+00:00', '2026-01-02T04:00:00+00:00')],
            'new': [('d' * 24, '2026-01-02T04:00:00+00:00', '2026-01-02T06:00:00+00:00')]}
    # 两边都在交接边界两侧各有一条进房和粉丝团推送；只保留旧库截止前、新库截止后的部分。
    events = {'old': [('2026-01-01T05:00:00+00:00', 50), ('2026-01-02T03:00:00+00:00', 44),
                      ('2026-01-02T05:00:00+00:00', 999)],
              'new': [('2026-01-02T03:30:00+00:00', 888), ('2026-01-02T05:00:00+00:00', 40)]}
    paths = {}
    for side in ('old', 'new'):
        root = tmp_path / side
        root.mkdir()
        db = tmp_path / f'{side}.sqlite3'
        with sqlite3.connect(db) as conn:
            conn.executescript(tables)
            for at, n in events[side]:
                conn.execute('INSERT INTO raw_events(room_id,received_utc,command,data) VALUES (?,?,?,?)',
                    ('1', at, 'ROOM_REAL_TIME_MESSAGE_UPDATE', json.dumps({'data': {'fans': 1, 'fans_club': n}})))
                conn.execute('INSERT INTO timeline(room_id,kind,at_utc,data) VALUES (?,?,?,?)', ('1', 'entry', at, '{}'))
        for key, start, end in plan[side]:
            d = root / key
            (d / 'public').mkdir(parents=True)
            (d / 'config.json').write_text(json.dumps({'room_id': '1', 'start_utc': start}))
            (d / 'workflow.json').write_text('{"stage":"completed"}')
            (d / 'public/snapshot.json').write_text(json.dumps({'session_start': start, 'updated_at': end}))
        paths[side] = (root, db)
    # 旧目录中截止后才开始的场次不纳入。
    late = paths['old'][0] / ('e' * 24)
    (late / 'public').mkdir(parents=True)
    (late / 'config.json').write_text(json.dumps({'room_id': '1', 'start_utc': '2026-01-02T05:00:00+00:00'}))
    (late / 'workflow.json').write_text('{"stage":"completed"}')
    history = [{'database': str(paths['old'][1]), 'session_root': str(paths['old'][0]), 'until': cutoff}]
    data = archive_data(*paths['new'], '1', '测试', now=datetime.fromisoformat('2026-01-02T18:00:00+08:00'),
                        history_sources=history)
    assert sorted(s['key'] for s in data['sessions']) == ['c' * 24, 'd' * 24]
    days = {d['date']: d for d in data['days']}
    assert (days['2026-01-01']['entries'], days['2026-01-01']['fans_club_close']) == (1, 50)
    assert (days['2026-01-02']['entries'], days['2026-01-02']['fans_club_close']) == (2, 40)
    assert (days['2026-01-02']['fans_club_change'], data['periods'][0]['fans_club_daily_mean']) == (-10, 45)
    assert 'PRIVATE' not in json.dumps(data) and str(tmp_path) not in json.dumps(data)
    with pytest.raises(ValueError):
        archive_data(*paths['new'], '1', '测试', history_sources=[{'database': 'x', 'session_root': 'y'}])
