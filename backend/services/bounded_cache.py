"""Small process-local TTL cache for non-sensitive derived counters/config."""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, Hashable, TypeVar


K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class BoundedTTLCache(Generic[K, V]):
    def __init__(self, *, ttl_seconds: float, max_entries: int = 1024) -> None:
        self.ttl_seconds = max(0.01, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._values: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> V | None:
        item = self._values.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= time.monotonic():
            self._values.pop(key, None)
            return None
        self._values.move_to_end(key)
        return value

    def set(self, key: K, value: V) -> None:
        self._values[key] = (time.monotonic() + self.ttl_seconds, value)
        self._values.move_to_end(key)
        while len(self._values) > self.max_entries:
            self._values.popitem(last=False)

    def invalidate(self, key: K) -> None:
        self._values.pop(key, None)

    def clear(self) -> None:
        self._values.clear()

    def __len__(self) -> int:
        return len(self._values)
