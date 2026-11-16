"""线程安全的 EPC 接收缓冲区：批内按 EPC 去重，取走时整批清空。"""

from __future__ import annotations

import threading
import time


class TagBuffer:
    def __init__(self, capacity: int = 5000) -> None:
        self._capacity = capacity
        self._items: dict[str, int | None] = {}
        self._dropped = 0
        self._stopped = False
        self._revision = 0
        self._cond = threading.Condition()

    @property
    def dropped(self) -> int:
        return self._dropped

    def size(self) -> int:
        with self._cond:
            return len(self._items)

    def add(self, epc: str, rssi: int | None) -> None:
        with self._cond:
            if epc in self._items:
                self._items[epc] = rssi
            else:
                if len(self._items) >= self._capacity:
                    self._items.pop(next(iter(self._items)))
                    self._dropped += 1
                self._items[epc] = rssi
                self._revision += 1
            self._cond.notify()

    def take_batch(self, quiet: float = 0.0) -> list[tuple[str, int | None]] | None:
        """阻塞直到有数据；停止且无数据时返回 None。

        quiet > 0 时，等到连续 quiet 秒没有新的唯一 EPC 加入再取走整批。
        """
        with self._cond:
            while not self._items and not self._stopped:
                self._cond.wait()
            if not self._items:
                return None
            if quiet > 0:
                seen = self._revision
                deadline = time.monotonic() + quiet
                while not self._stopped:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)
                    if self._revision != seen:
                        seen = self._revision
                        deadline = time.monotonic() + quiet
            batch = list(self._items.items())
            self._items.clear()
            return batch

    def requeue(self, batch: list[tuple[str, int | None]]) -> None:
        with self._cond:
            items = dict(batch)
            items.update(self._items)
            if len(items) > self._capacity:
                extra = len(items) - self._capacity
                for _ in range(extra):
                    items.pop(next(iter(items)))
                    self._dropped += 1
            self._items = items
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
