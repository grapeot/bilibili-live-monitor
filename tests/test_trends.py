import argparse
import hashlib
import json
import sqlite3

import pytest

from bilibili_live_monitor.trends import aggregate, configure_parser, main, snapshot, verify
from bilibili_live_monitor.trends_render import render

SINCE = '2030-01-01T10:00:00+08:00'
UNTIL = '2030-01-01T10:10:30+08:00'


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'monitor.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.executescript('''
            CREATE TABLE raw_events(id INTEGER PRIMARY KEY,room_id TEXT,received_utc TEXT,command TEXT,data TEXT);
            CREATE TABLE timeline(id INTEGER PRIMARY KEY,room_id TEXT,kind TEXT,source_key TEXT,data TEXT);
        ''')
    return path


def insert(path, *, at, uid='11', kind='entry', room='123456', event=None, family='SEND_GIFT_V2'):
    with sqlite3.connect(path) as connection:
        connection.execute('INSERT INTO raw_events(room_id,received_utc,command,data) VALUES(?,?,?,?)',
                           (room, at, 'IGNORED_EVENT', '{}'))
        row = {'received_utc': at, 'sender_uid': uid}
        if kind == 'gift':
            row['gift'] = {'event_type': family, 'event_id': event}
        connection.execute('INSERT INTO timeline(room_id,kind,source_key,data) VALUES(?,?,?,?)',
                           (room, kind, str(connection.total_changes) + at + str(uid), json.dumps(row)))


def stock(path, at, **values):
    with sqlite3.connect(path) as connection:
        connection.execute('INSERT INTO raw_events(room_id,received_utc,command,data) VALUES(?,?,?,?)',
                           ('123456', at, 'ROOM_REAL_TIME_MESSAGE_UPDATE', json.dumps({'data': values})))


def take(path, **kwargs):
    options = dict(room='123456', since=SINCE, until=UNTIL, display_zone='Asia/Shanghai', width=300,
                   gap_seconds=60, title=None)
    options.update(kwargs)
    data = snapshot(path, **options)
    data['summary'] = aggregate(data)
    return data


def args(db, output):
    parser = argparse.ArgumentParser()
    configure_parser(parser.add_subparsers(dest='command', required=True))
    return parser.parse_args(['trends', 'build', '--db', str(db), '--room', '123456',
                              '--since', SINCE, '--until', UNTIL, '--output', str(output)])


def test_half_open_range_offsets_microseconds_and_room_isolation(db):
    insert(db, at='2030-01-01T01:59:59.999999+00:00')
    insert(db, at=SINCE)
    insert(db, at='2030-01-01T02:10:29.999999+00:00', uid='12')
    insert(db, at=UNTIL, uid='13')
    insert(db, at=SINCE, uid='14', room='999999')
    data = take(db)
    assert data['summary']['entry_events'] == 2
    assert data['summary']['unique_entry_uids'] == 2
    assert [row['uid'] for row in data['source']['entries']] == ['11', '12']


def test_per_bin_uid_dedup_anonymous_and_partial_buckets(db):
    for at, uid in [('10:00:00', '11'), ('10:01:00', '11'), ('10:02:00', None),
                    ('10:05:00', '11'), ('10:10:00', '12')]:
        insert(db, at=f'2030-01-01T{at}+08:00', uid=uid)
    data = take(db)
    bins = data['summary']['bins']
    assert [b['unique_entries'] for b in bins] == [1, 1, 1]
    assert bins[0]['entries'] == 3 and bins[0]['anonymous_entries'] == 1
    assert data['summary']['unique_entry_uids'] == 2
    assert not bins[0]['partial'] and bins[-1]['partial']
    assert '（不完整桶）' in render(data)


def test_gift_orders_not_items_blind_results_and_sc_are_separate(db):
    for event, family, uid in [('one', 'SEND_GIFT', '11'), ('one', 'SEND_GIFT_V2', '11'),
                               ('one', 'SUPER_CHAT_MESSAGE', '11'), ('one', 'SEND_GIFT_V2', '12'),
                               (None, 'GUARD_BUY', '11')]:
        insert(db, at=SINCE, kind='gift', event=event, family=family, uid=uid)
    assert take(db)['summary']['gift_events'] == 4


def test_stock_totals_zero_missing_and_non_integer_values(db):
    stock(db, SINCE, fans=10, fans_club=8)
    stock(db, '2030-01-01T02:01:00Z', fans=9, fans_club=0)
    stock(db, '2030-01-01T02:02:00Z', fans=True, fans_club=-1)
    data = take(db)
    assert [s['followers'] for s in data['source']['stocks']] == [10, 9, None]
    assert [s['fans_club'] for s in data['source']['stocks']] == [8, 0, None]
    page = render(data)
    assert '最新 9' in page and '最新 0' in page
    assert 'stroke-dasharray' in page
    assert page.count('<section>') == 4
    assert '大航海人数' not in page


def test_empty_and_sparse_data_do_not_invent_zero_stocks(db):
    data = take(db)
    page = render(data)
    assert page.count('无观测数据') >= 4
    assert '最新 0' not in page
    assert len(data['summary']['gaps']) == 1
    assert data['summary']['gaps'][0]['start'] == data['summary']['bins'][0]['start']
    assert '无观测' in page


def test_read_only_input_and_reproducible_verify(db, tmp_path):
    insert(db, at=SINCE)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    output = tmp_path / 'report'
    assert main(args(db, output)) == 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert verify(output)['verified']
    assert main(args(db, output)) == 2
    assert verify(output)['verified']
    (output / 'index.html').write_text('tampered')
    with pytest.raises(ValueError, match='指纹'):
        verify(output)


def test_verify_recomputes_counts(db, tmp_path):
    output = tmp_path / 'report'
    assert main(args(db, output)) == 0
    data = json.loads((output / 'data.json').read_text())
    data['summary']['gift_events'] = 500
    (output / 'data.json').write_text(json.dumps(data))
    with pytest.raises(ValueError, match='统计'):
        verify(output)


def test_html_escapes_title_and_has_no_external_resources(db):
    data = take(db, title='<script>alert(1)</script>" &')
    page = render(data)
    assert '<script>' not in page
    assert '&lt;script&gt;' in page
    assert '<script src=' not in page and '<link ' not in page


@pytest.mark.parametrize('options', [dict(room='../123'), dict(since='2030-01-01T10:00:00'),
                                    dict(until=SINCE), dict(width=1), dict(width=3601),
                                    dict(gap_seconds=0), dict(display_zone='missing/timezone'),
                                    dict(until='2040-01-01T10:00:00+08:00')])
def test_invalid_scope_rejected(db, options):
    with pytest.raises((ValueError, KeyError)):
        take(db, **options)


def test_missing_database_not_created(tmp_path):
    path = tmp_path / 'missing.sqlite3'
    out = tmp_path / 'report'
    assert main(args(path, out)) == 2
    assert not path.exists() and not out.exists()


def test_existing_wal_writer_can_continue(db):
    with sqlite3.connect(db) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('INSERT INTO raw_events(room_id,received_utc,command,data) VALUES(?,?,?,?)',
                       ('123456', SINCE, 'UNCOMMITTED', '{}'))
        assert take(db)['summary']['raw_messages'] == 0
        writer.commit()
        assert take(db)['summary']['raw_messages'] == 1
