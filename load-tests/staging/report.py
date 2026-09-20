from __future__ import annotations

from typing import Any


THRESHOLDS: dict[str, tuple[str, float | int | bool]] = {
    "layers.core.save.p95_ms": ("max", 3000),
    "core.success_rate": ("min", .99),
    "layers.poll.heartbeat.p95_ms": ("max", 1000),
    "layers.poll.unread.p95_ms": ("max", 1000),
    "layers.poll.maintenance.p95_ms": ("max", 1000),
    "layers.poll.heartbeat.success_rate": ("min", .995),
    "layers.poll.unread.success_rate": ("min", .995),
    "layers.poll.maintenance.success_rate": ("min", .995),
    "layers.poll.query_data.success_rate": ("min", .995),
    "events.reconnect_success_rate": ("min", .99),
    # At 75 users, no more than 20% may reconnect within one minute.  This is
    # fixed before the run so a synchronized reconnect storm cannot be
    # explained away after the fact.
    "events.reconnect_peak_per_minute": ("max", 15),
    "resources.mysql.deadlock_delta": ("max", 0),
    "resources.mysql.lock_waits_peak": ("max", 0),
    "resources.mysql.row_lock_time_max_ms": ("max", 3000),
    "resources.mysql.pool_usage_ratio_peak": ("max", .90),
    "resources.redis.oom_delta": ("max", 0),
    "resources.redis.evicted_keys_delta": ("max", 0),
    "resources.redis.used_memory_ratio_peak": ("max", .85),
    "resources.kafka.lag_final": ("max", 0),
    "resources.kafka.growth_windows": ("max", 2),
    "resources.flink.checkpoint_completed_delta": ("min", 1),
    "resources.flink.checkpoint_failed_delta": ("max", 0),
    "resources.flink.checkpoint_duration_max_ms": ("max", 60000),
    "resources.derived_queue.pending_final": ("max", 0),
    "resources.derived_queue.drain_seconds": ("max", 120),
    "resources.production.healthy_all_samples": ("eq", True),
    "resources.production.restart_delta": ("max", 0),
    "resources.production.oom_killed_delta": ("max", 0),
    "resources.production.new_error_count": ("max", 0),
}


def _value(report: dict[str, Any], path: str) -> Any:
    """Read a metric path while preserving dotted Locust request names."""
    parts = path.split(".")
    value: Any = report
    offset = 0
    while offset < len(parts):
        if not isinstance(value, dict):
            return None
        for end in range(len(parts), offset, -1):
            key = ".".join(parts[offset:end])
            if key in value:
                value = value[key]
                offset = end
                break
        else:
            return None
    return value


def evaluate_report(report: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for path, (comparison, limit) in THRESHOLDS.items():
        actual = _value(report, path)
        if actual is None:
            checks.append({"name": path, "status": "unverified", "comparison": comparison, "limit": limit})
            continue
        if comparison == "max":
            passed = actual <= limit
        elif comparison == "min":
            passed = actual >= limit
        else:
            passed = actual == limit
        checks.append({
            "name": path,
            "status": "passed" if passed else "failed",
            "actual": actual,
            "comparison": comparison,
            "limit": limit,
        })
    return {
        "checks": checks,
        "passed": all(item["status"] == "passed" for item in checks),
        "failed": [item["name"] for item in checks if item["status"] == "failed"],
        "unverified": [item["name"] for item in checks if item["status"] == "unverified"],
    }
