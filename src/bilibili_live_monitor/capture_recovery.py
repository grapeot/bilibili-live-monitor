"""采音局部恢复的有界预算；短异常不升级为整页刷新。"""
import time
from collections import deque


class CaptureRecovery:
    observe_seconds = 15
    reload_seconds = 30
    healthy_seconds = 10

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.unhealthy_since = None
        self.good_seconds = 0
        self.local_attempted = False
        self.events = deque()
        self.totals = dict(page_reloads=0, local_restarts=0, invalid_windows=0,
                           valid_seconds=0.0, invalid_seconds=0.0, gap_seconds=0.0)

    def record(self, kind, amount=1):
        self.totals[kind] += amount
        self.events.append((self.clock(), kind, amount))

    def bad(self, *, immediate=False):
        now = self.clock()
        if self.unhealthy_since is None:
            self.unhealthy_since = now
        self.good_seconds = 0
        elapsed = now - self.unhealthy_since
        if elapsed >= self.reload_seconds:
            return 'reload'
        if immediate or (elapsed >= self.observe_seconds and not self.local_attempted):
            self.local_attempted = True
            return 'rebind'
        return 'observe'

    def good(self, seconds):
        self.good_seconds += seconds
        if self.good_seconds >= self.healthy_seconds:
            self.unhealthy_since = None
            self.local_attempted = False

    def new_page(self):
        self.unhealthy_since = None
        self.good_seconds = 0
        self.local_attempted = False

    def summary(self):
        cutoff = self.clock() - 3600
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()
        hour = dict.fromkeys(self.totals, 0)
        for _, kind, amount in self.events:
            hour[kind] += amount
        return {'totals': dict(self.totals), 'last_hour': hour,
                'scope': 'current_room_task',
                'state': 'observing' if self.unhealthy_since is not None else 'healthy'}


class PageRecoveryRequired(RuntimeError):
    """持续无效采音需要交给房间级刷新；不能在局部重试中吞掉。"""
