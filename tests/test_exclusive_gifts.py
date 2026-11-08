import json
import sqlite3

import pytest
from test_activity_summary import at, gift, window
from test_song_catalog import config

from bilibili_live_monitor.activity_summary import (
    accounting_errors,
    content_spans,
    summarize_activity,
    summarize_session,
)
from bilibili_live_monitor.song_catalog import catalog
from bilibili_live_monitor.song_gifts import performance_windows, summarize
from bilibili_live_monitor.time_utils import epoch


def test_rechunking_and_duplicate_windows_do_not_change_money():
    source = {'raw': [], 'items': [gift(0, 'a'), gift(29, 'b'), gift(30, 'c', amount=3000000),
                                  gift(59, 'd', amount=None), gift(80, 'e', count=None)]}
    whole = [window(0, 30), window(30, 60, '甲')]
    split = [window(0, 10), window(10, 30), window(30, 40, '甲'), window(40, 60, '甲')]
    expected = summarize_activity(source, whole, epoch(at(100)), start=epoch(at(0)), completed=True)
    for rows in (split, split + split, list(reversed(split))):
        assert summarize_activity(source, rows, epoch(at(100)), start=epoch(at(0)), completed=True) == expected
    total = summarize_session(source)
    for field in ('gift_count', 'gift_count_unknown', 'gift_amount_yuan', 'gift_amount_unknown'):
        assert sum(r[field] for r in expected) == total[field]
    assert expected[1]['gift_amount_yuan'] == 3000


def test_overlap_conflict_does_not_depend_on_chunking():
    spans = content_spans([window(0, 40), window(30, 60, '甲')], start=epoch(at(0)), end=epoch(at(60)))
    assert [(r['kind'], r['start'], r['end']) for r in spans] == [
        ('talk', epoch(at(0)), epoch(at(30))), ('unclassified', epoch(at(30)), epoch(at(40))),
        ('song', epoch(at(40)), epoch(at(60)))]
    assert content_spans([window(0, 100), window(30, 60, '甲')]) == content_spans(
        [window(0, 50), window(50, 100), window(30, 60, '甲')])


def test_received_only_and_cutoff_gifts_are_conserved():
    source = {'raw': [], 'items': [gift(60, 'edge'), gift(30, 'unknown', time_source='received', event_at_utc=None)]}
    result = summarize_activity(source, [window(0, 60, '甲')], epoch(at(60)), completed=True)
    assert result[0]['gift_count'] == 1
    assert result[-1]['time_unknown'] and result[-1]['gift_count'] == 1
    assert sum(r['gift_count'] for r in result) == summarize_session(source)['gift_count']
    snapshot = {'activity_accounting_version': 2, 'activity_summary': result, 'session_summary': summarize_session(source)}
    assert accounting_errors(snapshot) == []
    result[0]['gift_amount_yuan'] += 1
    assert accounting_errors(snapshot) == ['activity_accounting_mismatch:gift_amount_yuan']


def test_clipped_running_content_is_not_complete():
    source = {'raw': [], 'items': []}
    row = summarize_activity(source, [window(0, 100, '甲')], epoch(at(50)))[0]
    assert row['status'] == 'collecting'


def test_catalog_and_activity_agree_on_terminal_boundary(tmp_path):
    conf = config(tmp_path, activity=[window(0, 30, '甲'), window(30, 60, '乙')])
    snap_path = tmp_path/'anchor-a.json'
    snap = json.loads(snap_path.read_text())
    snap['updated_at'] = at(60)
    snap_path.write_text(json.dumps(snap))
    path = tmp_path/'gifts.db'
    events = [gift(30, 'boundary'), gift(60, 'cutoff')]
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE timeline(id INTEGER,room_id TEXT,kind TEXT,at_utc TEXT,data TEXT);'
                         'CREATE TABLE raw_events(id INTEGER,room_id TEXT,received_utc TEXT,command TEXT,data TEXT);')
        for i, (kind, when, data) in enumerate(events):
            db.execute('INSERT INTO timeline VALUES(?,?,?,?,?)',(i,'1',kind,when,json.dumps(data)))
    conf['sessions'][0]['gift_source'] = {'database': str(path), 'room_id': '1'}
    songs = {s['title']: s['gifts']['gift_count'] for s in catalog(conf)['songs']}
    assert songs == {'甲': 0, '乙': 2}
    rows = summarize_activity({'raw': [], 'items': events}, snap['activity'], epoch(at(60)), completed=True)
    assert [r['gift_count'] for r in rows] == [0, 2]


def test_same_start_conflict_and_empty_recording_are_unclassified():
    rows = [window(0, 30, '甲'), window(0, 30, '乙'),
            {**window(30, 60, '丙'), 'music_source': 'recording'}]
    spans = content_spans(rows, start=epoch(at(0)), end=epoch(at(90)))
    assert len(spans) == 1 and spans[0]['kind'] == 'unclassified'
    source = {'raw': [], 'items': [gift(10, 'a')]}
    assert summarize_activity(source, [], epoch(at(60)), start=epoch(at(0)))[0]['gift_count'] == 1


@pytest.mark.parametrize('segments', [[{'start': 10, 'end': 20}, {'start': 30, 'end': 40}],
                                    [{'start': at(10), 'end': at(20)}, {'start': at(30), 'end': at(40)}]])
def test_catalog_does_not_count_gap_or_padding(segments):
    performance = {'start': 0, 'end': 50, 'segments': segments}
    windows = performance_windows(performance)
    base = epoch(at(0)) if isinstance(segments[0]['start'], str) else 0
    events = [(base + t, 1, 1000, None, '') for t in (9, 10, 20, 25, 30, 40)]
    stats = summarize(events, windows, [(base, base+60)])
    assert stats['gift_count'] == 2 and stats['gift_amount_yuan'] == 2
