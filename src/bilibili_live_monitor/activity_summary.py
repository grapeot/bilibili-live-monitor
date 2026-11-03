"""按已标注的唱歌/杂谈区间汇总观测值，不将时间相关性解释为因果。"""
from bisect import bisect_left, bisect_right

from .live_share import epoch, iso


def content_blocks(activity):
    blocks = []
    for item in sorted(activity, key=lambda row: epoch(row['start'])):
        if item['label'] == 'singing' and item.get('music_source') == 'probable_live':
            kind, title = 'song', item.get('song')
        elif item['label'] in ('speaking', 'mixed'):
            kind, title = 'talk', None
        else:
            continue
        start, end = epoch(item['start']), epoch(item['end'])
        if end <= start:
            continue
        if (blocks and blocks[-1]['kind'] == kind and blocks[-1]['title'] == title
                and 0 <= start - blocks[-1]['end'] <= 2):
            blocks[-1]['end'] = end
        else:
            blocks.append({'kind': kind, 'title': title, 'start': start, 'end': end})
    # Brief acknowledgements inside the same identified song belong to that performance.
    index = 0
    while index + 2 < len(blocks):
        left, middle, right = blocks[index:index + 3]
        if (left['kind'] == right['kind'] == 'song' and left['title']
                and left['title'] == right['title'] and middle['kind'] == 'talk'
                and middle['end'] - middle['start'] <= 60
                and 0 <= middle['start'] - left['end'] <= 2
                and 0 <= right['start'] - middle['end'] <= 2):
            blocks[index:index + 3] = [{**left, 'end': right['end']}]
        else:
            index += 1
    return blocks


def nonnegative_integer(value):
    return value if type(value) is int and value >= 0 else None


def gift_amount(gift):
    """Return platform amount in thousandths of a yuan; no quantity multiplier for totals."""
    if gift.get('event_type') == 'SUPER_CHAT_MESSAGE':
        amount = nonnegative_integer(gift.get('price_yuan'))
        return amount * 1000 if amount is not None else None
    if gift.get('event_type') == 'GUARD_BUY':
        return nonnegative_integer(gift.get('guard_price_gold'))
    if gift.get('coin_type') == 'silver':
        return 0
    if gift.get('coin_type') == 'gold':
        return nonnegative_integer(gift.get('total_coin'))
    return None


def observations(source):
    stocks = sorted((epoch(at), data['data']['fans']) for at, command, data in source['raw']
                    if command == 'ROOM_REAL_TIME_MESSAGE_UPDATE'
                    and isinstance(data.get('data'), dict)
                    and nonnegative_integer(data['data'].get('fans')) is not None)
    gifts, seen = [], set()
    for index, (kind, at, data) in enumerate(source['items']):
        gift = data.get('gift')
        if kind != 'gift' or not isinstance(gift, dict):
            continue
        family = gift.get('event_type', '')
        family = 'SEND_GIFT' if family.startswith('SEND_GIFT') else family
        event = gift.get('event_id')
        key = ((family, gift.get('sender_uid'), event, gift.get('batch_index', 0))
               if event else ('source', data.get('source_key') or data.get('_record_id') or index))
        if key in seen:
            continue
        seen.add(key)
        count = nonnegative_integer(gift.get('num'))
        gifts.append((epoch(at), count, gift_amount(gift)))
    return stocks, gifts


def summarize_activity(source, activity, cutoff, *, completed=False):
    blocks = content_blocks(activity)
    reviewed_until = max((epoch(item['end']) for item in activity), default=0)
    stocks, gifts = observations(source)
    times = [time for time, _ in stocks]
    result = []
    for block in blocks:
        start, end = block['start'], block['end']
        target = end + 30
        observed_end = min(target, cutoff)
        start_index, end_index = bisect_right(times, start) - 1, bisect_left(times, observed_end) - 1
        change = stocks[end_index][1] - stocks[start_index][1] if start_index >= 0 and end_index >= 0 else None
        selected = [(count, amount) for at, count, amount in gifts if start <= at < observed_end]
        closed = completed or reviewed_until > end
        result.append({'kind': block['kind'], 'title': block['title'], 'start': iso(start), 'end': iso(end),
                       'window_end': iso(observed_end), 'followers_change': change,
                       'gift_count': sum(count for count, _ in selected if count is not None),
                       'gift_count_unknown': sum(count is None for count, _ in selected),
                       'gift_amount_yuan': sum(amount for _, amount in selected if amount is not None) / 1000,
                       'gift_amount_unknown': sum(amount is None for _, amount in selected),
                       'status': 'complete' if closed and cutoff >= target else
                       'partial' if completed else 'collecting'})
    return result


def summarize_session(source):
    stocks, gifts = observations(source)
    watched = sorted((epoch(at), packet['data']['num']) for at, command, packet in source['raw']
                     if command == 'WATCHED_CHANGE' and isinstance(packet.get('data'), dict)
                     and nonnegative_integer(packet['data'].get('num')) is not None)
    return {'followers_change': stocks[-1][1] - stocks[0][1] if stocks else None,
            'gift_count': sum(count for _, count, _ in gifts if count is not None),
            'gift_count_unknown': sum(count is None for _, count, _ in gifts),
            'gift_amount_yuan': sum(amount for _, _, amount in gifts if amount is not None) / 1000,
            'gift_amount_unknown': sum(amount is None for _, _, amount in gifts),
            'watched': watched[-1][1] if watched else None}
