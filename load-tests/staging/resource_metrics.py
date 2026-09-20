from __future__ import annotations

from typing import Any


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _delta(samples: list[dict[str, Any]], section: str, field: str) -> int:
    if not samples:
        return 0
    return _integer(samples[-1].get(section, {}).get(field)) - _integer(samples[0].get(section, {}).get(field))


def summarize_resource_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if len(samples) < 2:
        return {"sample_count": len(samples), "unverified": True}

    required = {
        "mysql": {"Threads_connected", "Threads_running", "Innodb_row_lock_current_waits",
                  "Innodb_row_lock_time_max", "Innodb_deadlocks"},
        "backend_pool": {"pool_count", "usage_ratio", "used", "max_size"},
        "redis": {"used_memory", "maxmemory", "oom_error_count", "evicted_keys",
                  "keyspace_hits", "keyspace_misses"},
        "kafka": {"lag"},
        "flink": {"checkpoint_completed", "checkpoint_failed", "checkpoint_duration_ms",
                  "backpressure_ratio"},
        "derived_queue": {"pending"},
        "production": {"healthy", "restart_count", "oom_killed_count", "error_count"},
    }
    missing: set[str] = set()
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            missing.add(f"sample[{index}]")
            continue
        for section, fields in required.items():
            payload = sample.get(section)
            if not isinstance(payload, dict):
                missing.add(f"sample[{index}].{section}")
                continue
            for field in fields:
                if field not in payload or payload[field] is None:
                    missing.add(f"sample[{index}].{section}.{field}")
    final_queue = samples[-1].get("derived_queue") if isinstance(samples[-1], dict) else None
    if not isinstance(final_queue, dict) or final_queue.get("drain_seconds") is None:
        missing.add(f"sample[{len(samples) - 1}].derived_queue.drain_seconds")
    if missing:
        return {"sample_count": len(samples), "unverified": True, "missing": sorted(missing)}

    mysql = [item.get("mysql", {}) for item in samples]
    backend_pool = [item.get("backend_pool", {}) for item in samples]
    redis = [item.get("redis", {}) for item in samples]
    kafka = [item.get("kafka", {}) for item in samples]
    flink = [item.get("flink", {}) for item in samples]
    queues = [item.get("derived_queue", {}) for item in samples]
    production = [item.get("production", {}) for item in samples]

    redis_ratios = []
    for item in redis:
        maximum = _integer(item.get("maxmemory"))
        redis_ratios.append((_integer(item.get("used_memory")) / maximum) if maximum else 0.0)
    hits = _delta(samples, "redis", "keyspace_hits")
    misses = _delta(samples, "redis", "keyspace_misses")
    lag_values = [_integer(item.get("lag")) for item in kafka]
    growth_windows = sum(
        1 for previous, current in zip(lag_values, lag_values[1:])
        if current > previous and current > 0
    )
    return {
        "sample_count": len(samples),
        "unverified": False,
        "mysql": {
            "connections_peak": max((_integer(item.get("Threads_connected")) for item in mysql), default=0),
            "threads_running_peak": max((_integer(item.get("Threads_running")) for item in mysql), default=0),
            "lock_waits_peak": max((_integer(item.get("Innodb_row_lock_current_waits")) for item in mysql), default=0),
            "row_lock_time_max_ms": max((_integer(item.get("Innodb_row_lock_time_max")) for item in mysql), default=0),
            "deadlock_delta": _delta(samples, "mysql", "Innodb_deadlocks"),
            "pool_usage_ratio_peak": max(
                (float(item.get("usage_ratio") or 0) for item in backend_pool), default=0.0,
            ),
            "pool_used_peak": max((_integer(item.get("used")) for item in backend_pool), default=0),
            "pool_max_size": max((_integer(item.get("max_size")) for item in backend_pool), default=0),
        },
        "redis": {
            "used_memory_ratio_peak": round(max(redis_ratios, default=0.0), 6),
            "oom_delta": _delta(samples, "redis", "oom_error_count"),
            "evicted_keys_delta": _delta(samples, "redis", "evicted_keys"),
            "hit_rate": round(hits / (hits + misses), 6) if hits + misses > 0 else None,
        },
        "kafka": {
            "lag_peak": max(lag_values, default=0),
            "lag_final": lag_values[-1] if lag_values else 0,
            "growth_windows": growth_windows,
        },
        "flink": {
            "checkpoint_completed_delta": _delta(samples, "flink", "checkpoint_completed"),
            "checkpoint_failed_delta": _delta(samples, "flink", "checkpoint_failed"),
            "checkpoint_duration_max_ms": max((_integer(item.get("checkpoint_duration_ms")) for item in flink), default=0),
            "backpressure_peak": max((float(item.get("backpressure_ratio") or 0) for item in flink), default=0.0),
        },
        "derived_queue": {
            "pending_peak": max((_integer(item.get("pending")) for item in queues), default=0),
            "pending_final": _integer(queues[-1].get("pending")) if queues else 0,
            "drain_seconds": _integer(queues[-1].get("drain_seconds")) if queues else 0,
        },
        "production": {
            "healthy_all_samples": all(bool(item.get("healthy")) for item in production),
            "restart_delta": _delta(samples, "production", "restart_count"),
            "oom_killed_delta": _delta(samples, "production", "oom_killed_count"),
            "new_error_count": _delta(samples, "production", "error_count"),
        },
    }
