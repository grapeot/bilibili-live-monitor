import pytest

from bilibili_live_monitor.activity_summary import (
    content_blocks,
    gift_amount,
    render_gifters,
    summarize_activity,
    summarize_gifters,
    summarize_session,
)
from bilibili_live_monitor.live_share import epoch, iso, make_snapshot, render_snapshot

START = '2030-01-01T00:00:00Z'


def at(seconds):
    return iso(epoch(START) + seconds)


def window(start, end, title=None):
    return {'start': at(start), 'end': at(end), 'label': 'singing' if title else 'speaking',
            'music_source': 'probable_live' if title else 'none', 'song': title}


def gift(second, ident, count=1, amount=1000, **extra):
    return ('gift', at(second), {'source_key': str(ident), 'gift': {
        'event_type': 'SEND_GIFT', 'event_id': str(ident), 'sender_uid': 'private-uid',
        'num': count, 'coin_type': 'gold', 'total_coin': amount, **extra}})


def fan(second, count):
    return (at(second), 'ROOM_REAL_TIME_MESSAGE_UPDATE', {'data': {'fans': count}})


def test_song_tail_window_and_overall_do_not_sum_overlapping_rows():
    source = {'raw': [fan(0, 100), fan(30, 101), fan(119, 105), fan(120, 109)], 'segments': [],
              'items': [gift(30, 'a', 2, 2000), gift(119, 'b', 3, 6000), gift(120, 'c')]}
    rows = summarize_activity(source, [window(0, 30), window(30, 90, '测试歌'), window(90, 150)], epoch(at(200)))
    assert [r['kind'] for r in rows] == ['talk', 'song', 'talk']
    assert rows[1]['followers_change'] == 4
    assert rows[1]['gift_count'] == 5 and rows[1]['gift_amount_yuan'] == 8
    assert rows[1]['window_end'] == at(120) and rows[1]['status'] == 'complete'
    total = summarize_session(source)
    assert total['gift_count'] == 6 and total['gift_amount_yuan'] == 9
    assert total['gift_count'] < sum(r['gift_count'] for r in rows)


def test_group_brief_talk_inside_song_but_preserve_long_talk_and_gaps():
    blocks = content_blocks([window(0, 30, '测试歌'), window(30, 60), window(60, 90, '测试歌'),
                             window(90, 180), window(180, 210, '测试歌'), window(220, 250, '测试歌')])
    assert [(b['kind'], b['start'], b['end']) for b in blocks] == [
        ('song', epoch(at(0)), epoch(at(90))), ('talk', epoch(at(90)), epoch(at(180))),
        ('song', epoch(at(180)), epoch(at(210))), ('song', epoch(at(220)), epoch(at(250)))]


def test_reported_currency_units_quantity_and_duplicate_events():
    assert gift_amount({'event_type': 'GUARD_BUY', 'num': 3, 'guard_price_gold': 198000}) == 198000
    assert gift_amount({'event_type': 'SUPER_CHAT_MESSAGE', 'price_yuan': 30}) == 30000
    assert gift_amount({'coin_type': 'silver', 'total_coin': 100000}) == 0
    assert gift_amount({'coin_type': 'gold', 'price': 500000, 'total_coin': 6000,
                        'blind_gift': {'original_gift_price': 6000}}) == 6000
    first = gift(10, 'same', 3, 6000)
    source = {'raw': [], 'segments': [], 'items': [first, first,
              gift(20, 'same', 2, 4000, event_type='SEND_GIFT_V2', batch_index=1),
              gift(21, 'missing', amount=None)]}
    total = summarize_session(source)
    assert total['gift_count'] == 6 and total['gift_amount_yuan'] == 10
    assert total['gift_amount_unknown'] == 1


def test_missing_followers_and_unfinished_tail_are_explicit():
    source = {'raw': [fan(40, 1)], 'segments': [], 'items': []}
    row = summarize_activity(source, [window(30, 90, '测试歌')], epoch(at(100)))[0]
    assert row['followers_change'] is None and row['status'] == 'collecting'
    assert row['window_end'] == at(100)
    assert summarize_activity(source, [window(30, 90, '测试歌')], epoch(at(100)), completed=True)[0]['status'] == 'partial'


@pytest.mark.parametrize('label,music_source', [('singing', 'recording'), ('unknown', 'uncertain')])
def test_excluded_following_content_still_closes_previous_song(label, music_source):
    following = {**window(30, 90), 'label': label, 'music_source': music_source}
    activity = [window(0, 30, '测试歌'), following]
    source = {'raw': [fan(0, 100), fan(50, 101)], 'items': [], 'segments': []}
    rows = summarize_activity(source, activity, epoch(at(90)))
    assert len(rows) == 1 and rows[0]['status'] == 'complete'
    assert rows[0]['window_end'] == at(60)
    assert summarize_activity(source, activity, epoch(at(45)))[0]['status'] == 'collecting'


def test_session_scope_watched_and_public_html_module_order():
    source = {'raw': [fan(1, 100), fan(100, 104), (at(80), 'WATCHED_CHANGE', {'data': {'num': 300}}),
                      (at(301), 'WATCHED_CHANGE', {'data': {'num': 999}})],
              'segments': [], 'items': [gift(10, 'yes'), gift(301, 'future')]}
    result = make_snapshot(source, start=START, now=at(300), label='合成直播', phase='live', health={})
    total = result['session_summary']
    assert total['followers_change'] == 4 and total['watched'] == 300
    assert total['gift_count'] == 1 and total['gift_amount_yuan'] == 1
    assert 'private-uid' not in str(result)
    html = render_snapshot(result)
    assert html.index('id="followups"') < html.index('id="activity-summary"') < html.index('id="gift-summary"') < html.index('id="session-summary"')


def test_gifters_group_by_identity_dedupe_and_match_session_totals():
    first = gift(10, 'one', count=2, amount=5000)
    first[2]['author'] = '<合成观众>'
    second = gift(20, 'two', count=1, amount=2000)
    second[2]['author'] = '改名观众'
    third = gift(30, 'three', count=3, amount=9000, sender_uid='other-private-uid')
    third[2]['author'] = '改名观众'
    source = {'raw':[], 'segments':[], 'items':[first, first, second, third]}
    rows = summarize_gifters(source)
    assert [row['amount_yuan'] for row in rows] == [9, 7]
    assert len(rows) == 2 and rows[0]['name'] == rows[1]['name'] == '改名观众'
    assert sum(row['gift_count'] for row in rows) == summarize_session(source)['gift_count'] == 6
    assert sum(row['amount_yuan'] for row in rows) == summarize_session(source)['gift_amount_yuan'] == 16
    assert 'uid' not in str(rows)
    rows[0]['name'] = '<script>alert(1)</script>'
    html = render_gifters(rows)
    assert '<script>' not in html and '&lt;script&gt;' in html


def test_gifters_keep_free_unknown_guard_and_superchat_values():
    source = {'raw':[], 'segments':[], 'items':[
        gift(1, 'guard', count=1, event_type='GUARD_BUY', guard_price_gold=198000),
        gift(2, 'sc', count=1, event_type='SUPER_CHAT_MESSAGE', price_yuan=30),
        gift(3, 'free', count=2, coin_type='silver'),
        gift(4, 'unknown', count=None, amount=None, sender_uid=None)]}
    rows = summarize_gifters(source)
    assert sum(row['amount_yuan'] for row in rows) == 228
    assert sum(row['amount_unknown'] for row in rows) == 1
    assert sum(row['gift_count_unknown'] for row in rows) == 1
    assert rows[-1]['name'] == '未识别观众（合计）'
