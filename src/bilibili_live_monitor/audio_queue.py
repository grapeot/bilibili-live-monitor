import asyncio
from collections import deque


class AudioQueue(asyncio.Queue):
    """单事件循环内同步移除房间任务，保留其他项顺序与join计数。"""

    def snapshot(self):
        return list(self._queue)

    def remove_where(self, predicate):
        removed, kept = [], deque()
        for item in self._queue:
            (removed if predicate(item) else kept).append(item)
        self._queue.clear()
        self._queue.extend(kept)
        for _ in removed:
            self.task_done()
        if removed:
            self._wakeup_next(self._putters)
        return removed
