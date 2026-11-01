import time
from datetime import datetime


def backoff_delay(attempt, base=2, maximum=300):
    """第N次连续失败后的等待：指数增长，达到上限后保持。"""
    return min(maximum, base * 2 ** min(max(0, attempt - 1), 30))


def remaining_delay(deadline):
    return max(0, datetime.fromisoformat(deadline).timestamp()-time.time()) if deadline else 0
