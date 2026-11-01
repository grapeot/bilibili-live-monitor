import asyncio
from collections import deque


class AudioQueue(asyncio.Queue):
    """单事件循环内同步移除房间任务，保留其他项顺序与join计数。"""

    def full(self):
        return self.maxsize > 0 and sum(item[1] is not None for item in self._queue) >= self.maxsize

    def put_control(self, item):
        """释放流的控制消息不占PCM容量，也不能在停止时被丢弃。"""
        self._put(item)
        self._unfinished_tasks += 1
        self._finished.clear()
        self._wakeup_next(self._getters)

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
