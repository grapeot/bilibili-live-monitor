"""滚动周期摘要与可选送礼占比；输出不含账号身份或配置。"""
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def period_overview(days, *, now=None, share_amounts=None, share_label=None):
    today = (now or datetime.now(ZoneInfo('Asia/Shanghai'))).astimezone(ZoneInfo('Asia/Shanghai')).date()
    result = []
    for count in (7, 30):
        start = today - timedelta(days=count - 1)
        selected = [row for row in days if start.isoformat() <= row['date'] <= today.isoformat()
                    and row.get('has_message_observations', row.get('has_observations'))]
        known = [row for row in selected if row.get('gift_amount_yuan') is not None]
        total = round(sum(row['gift_amount_yuan'] for row in known), 3) if known else None
        unknown = sum(row.get('gift_amount_unknown', 0) for row in selected)
        entries = [row['entries'] for row in selected if row.get('entries') is not None]
        item = {'days': count, 'start': start.isoformat(), 'end': today.isoformat(),
                'observed_days': len(entries), 'entries_daily_mean': sum(entries) / len(entries) if entries else None,
                'gift_amount_yuan': total, 'gift_amount_unknown': unknown}
        if share_label is not None:
            amounts = [(share_amounts or {}).get(row['date'], 0) for row in selected]
            own = sum(amount or 0 for amount in amounts)
            item['gift_share'] = {'label': share_label,
                                 'percentage': own / total * 100 if total and not unknown and None not in amounts else None}
        result.append(item)
    return result


def overall_share(days, amounts, label):
    observed = [row for row in days if row.get('has_observations')]
    total = sum(row.get('gift_amount_yuan') or 0 for row in observed)
    unknown = any(row.get('gift_amount_unknown') for row in observed)
    own = sum(amount or 0 for amount in amounts.values())
    return {'label': label, 'percentage': own / total * 100
            if total and not unknown and None not in amounts.values() else None}


def summary_gift_share(rows, label):
    """总体沿用已审核场次金额，避免遗漏只有历史汇总的场次。"""
    # 调用者须确认 amount_nonself 排除的账号集合与当前指定对象一致。
    amounts = [(row.get('metrics', {}).get('amount'), row.get('metrics', {}).get('amount_nonself'))
               for row in rows]
    if not amounts or any(type(value) not in (int, float) or not math.isfinite(value)
                          for pair in amounts for value in pair):
        return {'label': label, 'percentage': None}
    total = sum(pair[0] for pair in amounts)
    own = sum(pair[0] - pair[1] for pair in amounts)
    return {'label': label, 'percentage': own / total * 100 if total > 0 else None}
