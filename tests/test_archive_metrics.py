from copy import deepcopy

import pytest

from bilibili_live_monitor.archive_metrics import aggregate_metrics
from bilibili_live_monitor.archive_summary import summaries
from bilibili_live_monitor.time_utils import epoch

START = '2030-01-02T02:00:00+00:00'
END = '2030-01-02T02:02:00+00:00'
LOWER, UPPER = epoch(START), epoch(END)


def source():
    return {'raw': [(START, 'ONLINE_RANK_COUNT', {'data': {'online_count': 10}}),
                    ('2030-01-02T02:01:00+00:00', 'ONLINE_RANK_COUNT', {'data': {'online_count': 20}}),
                    (END, 'WATCHED_CHANGE', {'data': {'num': 42}})], 'items': []}


def test_live_session_visible_without_follower_push():
    data = source()
    original = deepcopy(data)
    rows = summaries(data, [{'key': 'example', 'start': START, 'end': END,
                             'status': 'live', 'has_result': True}])
    assert len(rows) == 1
    assert rows[0]['status'] == 'live'
    assert rows[0]['metrics']['followers_net'] is None
    assert rows[0]['metrics']['online_mean'] == 15
    assert rows[0]['metrics']['watched_last'] == 42
    assert data == original


@pytest.mark.parametrize('missing,fields', [
    ('ONLINE_RANK_COUNT', ['online_mean', 'online_peak']),
    ('WATCHED_CHANGE', ['watched_last']),
])
def test_missing_metric_does_not_block_other_metrics(missing, fields):
    data = source()
    data['raw'] = [row for row in data['raw'] if row[1] != missing]
    metrics = aggregate_metrics(data, LOWER, UPPER, {'123'})
    assert all(metrics[field] is None for field in fields)
    assert metrics['amount'] == metrics['amount_nonself'] == metrics['paid_donors_nonself'] == 0


def test_follower_delta_requires_two_in_session_observations():
    data = source()
    data['raw'].insert(0, ('2030-01-01T23:00:00+00:00', 'ROOM_REAL_TIME_MESSAGE_UPDATE', {'data': {'fans': 99}}))
    data['raw'].append((START, 'ROOM_REAL_TIME_MESSAGE_UPDATE', {'data': {'fans': 100}}))
    assert aggregate_metrics(data, LOWER, UPPER)['followers_net'] is None
    data['raw'].append((END, 'ROOM_REAL_TIME_MESSAGE_UPDATE', {'data': {'fans': 100}}))
    assert aggregate_metrics(data, LOWER, UPPER)['followers_net'] == 0
    data['raw'][-1][2]['data']['fans'] = 98
    assert aggregate_metrics(data, LOWER, UPPER)['followers_net'] == -2


def test_empty_source_is_unknown_not_zero():
    assert all(value is None for value in aggregate_metrics({'raw': [], 'items': []}, LOWER, UPPER, set()).values())


def test_online_observation_only_at_cutoff_has_no_mean():
    data = {'raw': [(END, 'ONLINE_RANK_COUNT', {'data': {'online_count': 0}})], 'items': []}
    metrics = aggregate_metrics(data, LOWER, UPPER)
    assert metrics['online_mean'] is None
    assert metrics['online_peak'] == 0


def gift(uid, event, amount):
    return ('gift', START, {'sender_uid': uid, 'source_key': event, 'gift': {
        'event_type': 'SEND_GIFT_V2', 'sender_uid': uid, 'event_id': event,
        'coin_type': 'gold', 'total_coin': amount, 'num': 1}})


def test_gift_dedup_exclusion_and_unknown_identity():
    data = source()
    data['items'] = [gift('123', 'a', 10000), gift('123', 'a', 10000), gift('456', 'b', 5000)]
    metrics = aggregate_metrics(data, LOWER, UPPER, {'123'})
    assert (metrics['amount'], metrics['amount_nonself'], metrics['paid_donors_nonself']) == (15, 5, 1)
    data['items'].append(gift(None, 'c', 1000))
    metrics = aggregate_metrics(data, LOWER, UPPER, {'123'})
    assert metrics['amount'] == 16
    assert metrics['amount_nonself'] is metrics['paid_donors_nonself'] is None


def test_unknown_gift_amount_does_not_hide_viewer_metrics():
    data = source()
    data['items'] = [gift('456', 'a', None)]
    metrics = aggregate_metrics(data, LOWER, UPPER, {'123'})
    assert metrics['amount'] is metrics['amount_nonself'] is metrics['paid_donors_nonself'] is None
    assert metrics['online_mean'] == 15
    assert metrics['watched_last'] == 42


def test_out_of_range_and_invalid_observations_not_used():
    data = source()
    data['raw'] += [('2030-01-02T02:03:00+00:00', 'WATCHED_CHANGE', {'data': {'num': 999}}),
                    (END, 'ONLINE_RANK_COUNT', {'data': {'online_count': True}}),
                    (END, 'ROOM_REAL_TIME_MESSAGE_UPDATE', {'data': {'fans': -1}})]
    metrics = aggregate_metrics(data, LOWER, UPPER)
    assert metrics['watched_last'] == 42
    assert metrics['online_peak'] == 20
    assert metrics['followers_net'] is None
