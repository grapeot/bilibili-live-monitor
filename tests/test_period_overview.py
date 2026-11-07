from datetime import datetime

import pytest

from bilibili_live_monitor.period_overview import period_overview, summary_gift_share


def day(date, entries, amount, observed=True, unknown=0):
    return dict(date=date, entries=entries, gift_amount_yuan=amount,
                has_observations=observed, gift_amount_unknown=unknown)


def test_rolling_beijing_dates_missing_days_and_real_zero():
    # UTC Jan 7 is already Jan 8 in Beijing; both windows include today.
    rows = [day('2026-01-08', 20, 10), day('2026-01-02', 0, 0),
            day('2026-01-03', None, None, False), day('2026-01-01', 100, 50),
            day('2025-12-09', 900, 900), day('2026-01-09', 500, 500)]
    week, month = period_overview(rows, now=datetime.fromisoformat('2026-01-07T20:00:00+00:00'))
    assert (week['start'], week['end'], week['observed_days']) == ('2026-01-02', '2026-01-08', 2)
    assert (week['entries_daily_mean'], week['gift_amount_yuan']) == (10, 10)
    assert (month['observed_days'], month['entries_daily_mean'], month['gift_amount_yuan']) == (3, 40, 60)


def test_missing_amount_and_optional_share():
    now = datetime.fromisoformat('2026-01-08T00:00:00+08:00')
    result = period_overview([], now=now)
    assert all(row['entries_daily_mean'] is None and row['gift_amount_yuan'] is None for row in result)
    assert all('gift_share' not in row for row in result)
    result = period_overview([day('2026-01-08', 0, 10, unknown=1)], now=now,
                             share_amounts={'2026-01-08': 5}, share_label='测试观众')
    assert result[0]['gift_share'] == {'label': '测试观众', 'percentage': None}


@pytest.mark.parametrize('rows,expected', [
    ([{'metrics': {'amount': 100, 'amount_nonself': 75}},
      {'metrics': {'amount': 50, 'amount_nonself': 0}}], 50),
    ([{'metrics': {'amount': 0, 'amount_nonself': 0}}], None),
    ([{'metrics': {'amount': None, 'amount_nonself': 0}}], None),
])
def test_overall_preserves_audited_history(rows, expected):
    assert summary_gift_share(rows, '测试观众') == {'label': '测试观众', 'percentage': expected}
