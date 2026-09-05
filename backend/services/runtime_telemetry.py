"""Small in-process counters for deprecated runtime paths."""

from __future__ import annotations

from collections import Counter


_counters: Counter[str] = Counter()


def increment(name: str, amount: int = 1) -> None:
    _counters[str(name)] += max(0, int(amount))


def snapshot() -> dict[str, int]:
    return {key: int(value) for key, value in _counters.items()}
