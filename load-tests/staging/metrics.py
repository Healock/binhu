from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class LayerMetrics:
    total: int = 0
    failures: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def observe(self, latency_ms: float, failed: bool = False) -> None:
        self.total += 1
        self.failures += int(failed)
        self.latencies_ms.append(float(latency_ms))

    def merge(self, other: "LayerMetrics") -> None:
        self.total += other.total
        self.failures += other.failures
        self.latencies_ms.extend(other.latencies_ms)

    def summary(self) -> dict[str, float | int | None]:
        values = sorted(self.latencies_ms)

        def percentile(fraction: float) -> float | None:
            if not values:
                return None
            index = max(0, min(len(values) - 1, math.ceil(len(values) * fraction) - 1))
            return round(values[index], 2)

        return {
            "requests": self.total,
            "failures": self.failures,
            "success_rate": round((self.total - self.failures) / self.total, 6) if self.total else None,
            "p50_ms": percentile(.50),
            "p95_ms": percentile(.95),
            "p99_ms": percentile(.99),
        }


@dataclass
class StagingMetrics:
    layers: dict[str, LayerMetrics] = field(default_factory=lambda: defaultdict(LayerMetrics))
    stream_connections: int = 0
    stream_current: int = 0
    stream_reconnect_attempts: int = 0
    stream_reconnect_successes: int = 0
    stream_reconnect_timestamps: list[float] = field(default_factory=list)
    expected_conflicts: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe(self, layer: str, latency_ms: float, failed: bool = False) -> None:
        with self._lock:
            self.layers[layer].observe(latency_ms, failed)

    def observe_expected_conflict(self) -> None:
        with self._lock:
            self.expected_conflicts += 1

    def stream_opened(self, *, reconnect: bool) -> None:
        with self._lock:
            self.stream_connections += 1
            self.stream_current += 1
            if reconnect:
                self.stream_reconnect_successes += 1

    def stream_closed(self) -> None:
        with self._lock:
            self.stream_current = max(0, self.stream_current - 1)

    def stream_reconnect_started(self, observed_at: float | None = None) -> None:
        with self._lock:
            self.stream_reconnect_attempts += 1
            self.stream_reconnect_timestamps.append(observed_at if observed_at is not None else time.time())

    def _aggregate(self, prefix: str) -> LayerMetrics:
        aggregate = LayerMetrics()
        for name, value in self.layers.items():
            if name.startswith(prefix):
                aggregate.merge(value)
        return aggregate

    def report(self) -> dict[str, object]:
        with self._lock:
            reconnect_rate = (
                self.stream_reconnect_successes / self.stream_reconnect_attempts
                if self.stream_reconnect_attempts else 1.0
            )
            return {
                "layers": {name: value.summary() for name, value in sorted(self.layers.items())},
                "core": self._aggregate("core.").summary(),
                "polling": self._aggregate("poll.").summary(),
                "events": {
                    "connections_opened": self.stream_connections,
                    "current_connections": self.stream_current,
                    "reconnect_attempts": self.stream_reconnect_attempts,
                    "reconnect_successes": self.stream_reconnect_successes,
                    "reconnect_success_rate": round(reconnect_rate, 6),
                    "reconnect_peak_per_minute": self._reconnect_peak_per_minute(),
                },
                "expected_conflicts": self.expected_conflicts,
            }

    def _reconnect_peak_per_minute(self) -> int:
        if not self.stream_reconnect_timestamps:
            return 0
        buckets: dict[int, int] = defaultdict(int)
        for observed_at in self.stream_reconnect_timestamps:
            buckets[int(observed_at // 60)] += 1
        return max(buckets.values(), default=0)


GLOBAL_METRICS = StagingMetrics()
