import json

import pytest

from bilibili_live_monitor.archive_summary import shared_summaries, summaries


def test_weighted_online_and_session_boundaries():
    source = {'items': [], 'raw': [
        ('2026-01-01T23:00:00Z', 'ONLINE_RANK_COUNT', {'data': {'online_count': 10}}),
        ('2026-01-02T00:00:00Z', 'ONLINE_RANK_COUNT', {'data': {'online_count': 30}}),
        ('2026-01-02T00:00:00Z', 'WATCHED_CHANGE', {'data': {'num': 123}}),
        ('2026-01-02T02:00:00Z', 'WATCHED_CHANGE', {'data': {'num': 999}})]}
    sessions = [{'key': 'test', 'start': '2026-01-01T23:00:00Z', 'end': '2026-01-02T01:00:00Z',
                 'status': 'completed', 'has_result': True}]
    row = summaries(source, sessions)[0]
    assert row['date'] == '2026-01-02'
    assert row['metrics']['online_mean'] == 20
    assert row['metrics']['online_peak'] == 30
    assert row['metrics']['watched_last'] == 123
    assert row['metrics']['paid_donors_nonself'] is None


def test_shared_file_is_authoritative_includes_old_sessions_and_projects_private_fields(tmp_path):
    path = tmp_path / 'summary.json'
    path.write_text(json.dumps({'sessions': [
        {'date': '2026-01-01', 'session': '晚间场', 'hours': 2, 'start_utc': 'start',
         'metrics': {'online_mean': 12.5, 'watched_last': 987, 'amount_nonself': 10},
         'url': '/private-link/', 'private': '/private/path'},
        {'date': '2025-01-01', 'session': '早场', 'hours': 1, 'metrics': {'watched_last': 42}}]}))
    rows = shared_summaries(path, [{'start_utc': 'start', 'key': 'local', 'has_result': True}])
    assert rows[0]['metrics']['watched_last'] == 987
    assert rows[0]['key'] == 'local' and rows[0]['has_result']
    assert rows[1]['metrics']['watched_last'] == 42 and rows[1]['key'] is None
    assert rows[1]['session'] == '早场'
    assert 'private' not in json.dumps(rows)


def test_unobserved_session_is_not_zero_gifts():
    session = {'key': 'new', 'start': '2026-01-01T00:00:00Z', 'end': '2026-01-01T01:00:00Z',
               'status': 'live', 'has_result': True}
    assert summaries({'items': [], 'raw': []}, [session])[0]['metrics']['amount'] is None
    source = {'items': [('entry', '2026-01-01T00:05:00Z', {})], 'raw': []}
    assert summaries(source, [session])[0]['metrics']['amount'] == 0


def test_shared_metadata_rejects_nested_fields(tmp_path):
    path = tmp_path / 'summary.json'
    path.write_text(json.dumps({'sessions': [{'date': {'private': 'value'}, 'metrics': {}}]}))
    with pytest.raises(ValueError, match='格式无效'):
        shared_summaries(path, [])
