"""按已标注的唱歌/杂谈区间汇总观测值，不将时间相关性解释为因果。"""
from bisect import bisect_left, bisect_right

from .time_utils import epoch, iso


def content_blocks(activity, *, include_segments=False):
    blocks = []
    for item in sorted(activity, key=lambda row: epoch(row['start'])):
        if item.get('music_source') == 'recording':
            continue
        if item['label'] == 'singing' and item.get('music_source') == 'probable_live':
            kind, title = 'song', item.get('song')
        elif item['label'] in ('speaking', 'mixed'):
            kind, title = 'talk', None
        else:
            continue
        start, end = epoch(item['start']), epoch(item['end'])
        if end <= start:
            continue
        # 按展示行相邻关系分组；segments仍保留缺录/隐藏内容，不能据外包络涂满覆盖。
        if blocks and blocks[-1]['kind'] == kind and blocks[-1]['title'] == title:
            blocks[-1]['end'] = max(blocks[-1]['end'], end)
            blocks[-1]['segments'].append({'start': start, 'end': end})
        else:
            blocks.append({'kind': kind, 'title': title, 'start': start, 'end': end,
                           'segments': [{'start': start, 'end': end}]})
    return [{k: v for k, v in block.items() if include_segments or k != 'segments'}
            for block in blocks]


def nonnegative_integer(value):
    return value if type(value) is int and value >= 0 else None


def gift_amount(gift):
    """Return platform amount in thousandths of a yuan; no quantity multiplier for totals."""
    if gift.get('event_type') == 'SUPER_CHAT_MESSAGE':
        amount = nonnegative_integer(gift.get('price_yuan'))
        if amount is not None:
            return amount * 1000
        return nonnegative_integer(gift.get('total_coin'))
    if gift.get('event_type') == 'GUARD_BUY':
        return nonnegative_integer(gift.get('guard_price_gold'))
    if gift.get('coin_type') == 'silver':
        return 0
    if gift.get('coin_type') == 'gold':
        return nonnegative_integer(gift.get('total_coin'))
    return None


def gift_observations(source, *, platform_uid=False):
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
        from .identity import analysis_key
        uid = (analysis_key(data) if data.get('analysis_identity_basis') == 'nickname'
               else gift.get('sender_uid') or data.get('sender_uid'))
        if platform_uid:
            uid = gift.get('sender_uid') or data.get('sender_uid')
            if (data.get('sender_hidden') or data.get('identity_status') == 'unresolved'
                    or not str(uid).isdigit() or int(uid) <= 0):
                uid = None
        gifts.append((epoch(at), count, gift_amount(gift), uid, str(data.get('author') or '')[:200]))
    return gifts


def observations(source):
    stocks = sorted((epoch(at), data['data']['fans']) for at, command, data in source['raw']
                    if command == 'ROOM_REAL_TIME_MESSAGE_UPDATE'
                    and isinstance(data.get('data'), dict)
                    and nonnegative_integer(data['data'].get('fans')) is not None)
    gifts = [item[:3] for item in gift_observations(source)]
    return stocks, gifts


def summarize_gifters(source):
    """Group by private identity; publish only names and aggregate totals."""
    people = {}
    for at, count, amount, uid, name in gift_observations(source):
        key = ('uid', str(uid)) if uid else ('unknown',)
        row = people.setdefault(key, {'name': name if uid and name else
                                     '未命名观众' if uid else '未识别观众（合计）',
                                     'gift_count':0, 'gift_count_unknown':0,
                                     'amount_milli':0, 'amount_unknown':0})
        if uid and name:
            row['name'] = name
        row['gift_count'] += count or 0
        row['gift_count_unknown'] += count is None
        row['amount_milli'] += amount or 0
        row['amount_unknown'] += amount is None
    return [{'name':row['name'], 'gift_count':row['gift_count'],
             'gift_count_unknown':row['gift_count_unknown'],
             'amount_yuan':row['amount_milli']/1000, 'amount_unknown':row['amount_unknown']}
            for row in sorted(people.values(), key=lambda item:-item['amount_milli'])]


def render_gifters(rows):
    import html

    body = ''.join('<tr><td>'+html.escape(row['name'])+'</td><td>'+str(row['gift_count'])+
                   ('+' if row['gift_count_unknown'] else '')+'</td><td>'+f'{row["amount_yuan"]:.2f}'+
                   ('+' if row['amount_unknown'] else '')+'</td></tr>' for row in rows)
    return ('<section id="gift-summary"><h2>每人送礼汇总</h2>'
            '<div class="activity-table"><table><thead><tr><th>观众</th><th>礼物件数</th>'
            '<th>累计金额（元）</th></tr></thead><tbody>'+ (body or '<tr><td colspan="3">暂无礼物记录</td></tr>')+
            '</tbody></table></div><p>按礼物平台标价汇总，包含盲盒结果；非主播实收。'
            '“+”表示另有金额或数量未知的礼物。</p></section>')


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
