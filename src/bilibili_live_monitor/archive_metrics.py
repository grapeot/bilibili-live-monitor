"""场次指标独立计算；尚无观测的字段为未知，不阻止场次展示。"""
import math

from .activity_summary import gift_observations, nonnegative_integer
from .time_utils import epoch


def aggregate_metrics(source, lower, upper, excluded_uids=None):
    """读取同房间来源，仅使用闭区间内观测；排除名单未配置时不计算归属指标。"""
    if not (math.isfinite(lower) and math.isfinite(upper) and lower <= upper):
        raise ValueError('invalid_metric_time_range')
    raw = [(epoch(at), command, packet.get('data')) for at, command, packet in source['raw']
           if lower <= epoch(at) <= upper]

    def points(command, field):
        return sorted((at, data[field]) for at, cmd, data in raw
                      if cmd == command and isinstance(data, dict)
                      and nonnegative_integer(data.get(field)) is not None)

    online = points('ONLINE_RANK_COUNT', 'online_count')
    watched = points('WATCHED_CHANGE', 'num')
    fans = points('ROOM_REAL_TIME_MESSAGE_UPDATE', 'fans')
    weighted = seconds = 0
    for index, (at, value) in enumerate(online):
        end = online[index + 1][0] if index + 1 < len(online) else upper
        duration = max(0, end - at)
        weighted += value * duration
        seconds += duration
    items = [(kind, at, item) for kind, at, item in source['items'] if lower <= epoch(at) <= upper]
    gifts = gift_observations({'items': items}, platform_uid=True)
    observed = bool(raw or items or any(
        lower < epoch(segment['end_utc']) and epoch(segment['start_utc']) < upper
        for segment in source.get('segments', [])))
    known_amount = observed and all(gift[2] is not None for gift in gifts)
    amount = round(sum(gift[2] for gift in gifts) / 1000, 2) if known_amount else None
    others = donors = None
    if excluded_uids is not None and known_amount and all(not gift[2] or gift[3] is not None for gift in gifts):
        excluded = set(map(str, excluded_uids))
        selected = [gift for gift in gifts if str(gift[3]) not in excluded]
        others = round(sum(gift[2] for gift in selected) / 1000, 2)
        donors = len({str(gift[3]) for gift in selected if gift[2] > 0})
    return {'online_mean': weighted / seconds if seconds else None,
            'online_peak': max((value for _, value in online), default=None),
            'watched_last': watched[-1][1] if watched else None,
            'followers_net': fans[-1][1] - fans[0][1] if len(fans) >= 2 else None,
            'amount': amount, 'amount_nonself': others, 'paid_donors_nonself': donors}
