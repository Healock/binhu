"""Low-overhead platform performance and congestion telemetry.

Request bodies, query values, user identifiers and business records are never
collected. Hot-path observations stay in a bounded in-memory rolling window,
so the operations center can inspect recent history without writing one
database row per request.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


MAX_RAW_SAMPLES = 120_000
RAW_RETENTION_SECONDS = 60 * 60

ENDPOINT_GROUP_LABELS = {
    "login": "登录",
    "task_list": "任务列表与详情",
    "task_save": "任务自动保存",
    "result_save": "核查结果保存",
    "bulk_assignment": "批量分配",
    "address_matching": "地址匹配",
    "file_operation": "导入与导出",
    "operations": "运维读取",
    "other": "其他业务接口",
}


@dataclass(slots=True)
class RequestSample:
    observed_at: float
    method: str
    route: str
    group: str
    latency_ms: float
    status_code: int
    inflight: int
    cancelled: bool = False


def _safe_route(path: str) -> str:
    """Remove accidental identifiers if routing metadata is unavailable."""
    value = str(path or "/")[:255]
    value = re.sub(r"/[0-9]+(?=/|$)", "/{id}", value)
    value = re.sub(
        r"/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}(?=/|$)",
        "/{id}",
        value,
    )
    return value


def endpoint_group(method: str, route: str) -> str:
    method = method.upper()
    path = route.lower()
    if path == "/api/auth/login":
        return "login"
    if any(value in path for value in ("assign", "distribution", "allocate")):
        return "bulk_assignment"
    if any(value in path for value in ("small-communit", "address-match", "police-address")):
        return "address_matching"
    if any(value in path for value in ("export", "import", "upload", "download")):
        return "file_operation"
    if path.startswith("/api/admin/ops"):
        return "operations"
    if method in {"POST", "PUT", "PATCH"} and any(
        value in path for value in ("result", "review", "judgment")
    ):
        return "result_save"
    if method in {"POST", "PUT", "PATCH", "DELETE"} and any(
        value in path for value in ("query", "task", "mobile")
    ):
        return "task_save"
    if any(value in path for value in ("query", "task", "mobile")):
        return "task_list"
    return "other"


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 1)


def _aggregate(samples: list[RequestSample]) -> dict[str, Any]:
    latencies = [sample.latency_ms for sample in samples]
    count = len(samples)
    error_count = sum(sample.status_code >= 500 for sample in samples)
    return {
        "requests": count,
        "average_ms": round(sum(latencies) / count, 1) if count else 0.0,
        "p50_ms": _percentile(latencies, 0.50),
        "p95_ms": _percentile(latencies, 0.95),
        "p99_ms": _percentile(latencies, 0.99),
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
        "errors_5xx": error_count,
        "error_rate": round(error_count * 100 / count, 2) if count else 0.0,
        "conflicts_409": sum(sample.status_code == 409 for sample in samples),
        "timeouts": sum(sample.status_code in {408, 504} for sample in samples),
        "cancelled": sum(sample.cancelled for sample in samples),
        "inflight_peak": max((sample.inflight for sample in samples), default=0),
    }


class PlatformPerformanceMetrics:
    def __init__(self) -> None:
        self.started_at = datetime.now(timezone.utc)
        self.samples: deque[RequestSample] = deque(maxlen=MAX_RAW_SAMPLES)
        self.loop_lag: deque[tuple[float, float]] = deque(maxlen=7200)
        self.inflight = 0
        self.peak_inflight = 0
        self._last_state = "normal"
        self._last_pressure_at: float | None = None

    def begin_request(self) -> tuple[float, int]:
        self.inflight += 1
        self.peak_inflight = max(self.peak_inflight, self.inflight)
        return time.perf_counter(), self.inflight

    def finish_request(
        self,
        *,
        started_at: float,
        method: str,
        route: str,
        status_code: int,
        inflight: int,
        cancelled: bool = False,
    ) -> None:
        self.inflight = max(0, self.inflight - 1)
        safe_route = _safe_route(route)
        self.samples.append(
            RequestSample(
                observed_at=time.time(),
                method=method.upper()[:10],
                route=safe_route,
                group=endpoint_group(method, safe_route),
                latency_ms=max(0.0, (time.perf_counter() - started_at) * 1000),
                status_code=int(status_code),
                inflight=max(0, int(inflight)),
                cancelled=cancelled,
            )
        )
        self._trim_raw()

    def observe_loop_lag(self, lag_ms: float) -> None:
        self.loop_lag.append((time.time(), max(0.0, lag_ms)))

    def _trim_raw(self) -> None:
        cutoff = time.time() - RAW_RETENTION_SECONDS
        while self.samples and self.samples[0].observed_at < cutoff:
            self.samples.popleft()

    def recent_samples(self, minutes: int) -> list[RequestSample]:
        cutoff = time.time() - max(1, minutes) * 60
        return [sample for sample in self.samples if sample.observed_at >= cutoff]

    def recent_loop_lag(self, minutes: int) -> list[float]:
        cutoff = time.time() - max(1, minutes) * 60
        return [lag for observed_at, lag in self.loop_lag if observed_at >= cutoff]

    def resolve_state(
        self,
        summary: dict[str, Any],
        *,
        loop_lag_ms: float,
        pool_pressure: float,
        mysql_threads_running: int,
        mysql_lock_waits: int,
        background: dict[str, Any],
    ) -> tuple[str, list[dict[str, str]]]:
        signals: list[dict[str, str]] = []

        def signal(level: str, code: str, title: str, detail: str, action: str, tab: str) -> None:
            signals.append({
                "level": level,
                "code": code,
                "title": title,
                "detail": detail,
                "recommended_action": action,
                "action_tab": tab,
            })

        if summary["p95_ms"] > 3000:
            signal("critical", "latency", "请求响应明显变慢", f"最近窗口 P95 为 {summary['p95_ms']:.0f} 毫秒。", "查看下方最慢接口，再到系统日志核对同一时段异常。", "logs")
        elif summary["p95_ms"] > 1500:
            signal("warning", "latency", "请求响应开始变慢", f"最近窗口 P95 为 {summary['p95_ms']:.0f} 毫秒。", "先查看最慢接口排行；若持续两个窗口，再检查数据库和后台任务。", "performance")
        if summary["error_rate"] > 2:
            signal("critical", "errors", "服务端错误偏高", f"非预期 5xx 占 {summary['error_rate']:.2f}%。", "进入系统日志，按当前时间检查后端错误。", "logs")
        elif summary["error_rate"] > 0.5:
            signal("warning", "errors", "服务端出现零星错误", f"非预期 5xx 占 {summary['error_rate']:.2f}%。", "进入系统日志确认错误是否集中在同一接口。", "logs")
        if loop_lag_ms > 500:
            signal("critical", "event_loop", "应用线程存在阻塞", f"事件循环最大延迟 {loop_lag_ms:.0f} 毫秒。", "检查最慢接口和后端日志，重点排查同步计算或阻塞式调用。", "logs")
        elif loop_lag_ms > 200:
            signal("warning", "event_loop", "应用线程有短时阻塞", f"事件循环最大延迟 {loop_lag_ms:.0f} 毫秒。", "观察下一窗口；若持续出现，检查最慢接口。", "performance")
        if pool_pressure >= 1:
            signal("critical", "db_pool", "数据库连接池已占满", "至少一个业务连接池没有空闲连接。", "进入数据库页核对 MySQL 连接，并暂停非必要的大批量后台任务。", "databases")
        elif pool_pressure >= 0.8:
            signal("warning", "db_pool", "数据库连接池接近占满", f"最高连接池占用 {pool_pressure * 100:.0f}%。", "观察后台任务占用；避免同时启动多个导入、归档或备份任务。", "databases")
        if mysql_threads_running > 30 or mysql_lock_waits > 0:
            signal("critical", "mysql_blocking", "MySQL 存在阻塞信号", f"运行线程 {mysql_threads_running}，当前锁等待 {mysql_lock_waits}。", "进入数据库页核对连接状态，并在系统日志定位长事务来源。", "databases")
        if background.get("oldest_active_seconds", 0) >= 300 or background.get("queued_count", 0) >= 20:
            signal("warning", "background_backlog", "后台任务出现积压", f"排队 {background.get('queued_count', 0)} 个，最久任务已运行或等待 {background.get('oldest_active_seconds', 0)} 秒。", "查看下方占用明细和右下角后台任务；按任务建议处理，不要重复提交。", "performance")

        critical = any(item["level"] == "critical" for item in signals)
        warning = any(item["level"] == "warning" for item in signals)
        now = time.time()
        if critical:
            state = "congested"
        elif warning:
            state = "busy"
        elif summary["requests"] < 5:
            state = "warming_up"
        elif self._last_state in {"busy", "congested"} and self._last_pressure_at and now - self._last_pressure_at < 120:
            state = "recovering"
        else:
            state = "normal"
        if state in {"busy", "congested"}:
            self._last_pressure_at = now
        self._last_state = state
        return state, signals


performance_metrics = PlatformPerformanceMetrics()


def _iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _timeline(samples: list[RequestSample], minutes: int) -> list[dict[str, Any]]:
    interval = 15 if minutes <= 15 else 60 if minutes <= 120 else 300
    now = time.time()
    first = math.floor((now - minutes * 60) / interval) * interval
    buckets: dict[int, list[RequestSample]] = defaultdict(list)
    for sample in samples:
        buckets[math.floor(sample.observed_at / interval) * interval].append(sample)
    result = []
    cursor = first
    while cursor <= now:
        aggregate = _aggregate(buckets.get(int(cursor), []))
        result.append({"bucket_at": _iso_from_timestamp(cursor), **aggregate})
        cursor += interval
    return result[-240:]


def _endpoint_rows(samples: list[RequestSample]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[RequestSample]] = defaultdict(list)
    for sample in samples:
        grouped[(sample.group, sample.method, sample.route)].append(sample)
    rows = []
    for (group, method, route), values in grouped.items():
        rows.append({
            "group": group,
            "group_label": ENDPOINT_GROUP_LABELS.get(group, group),
            "method": method,
            "route": route,
            **_aggregate(values),
        })
    rows.sort(key=lambda row: (row["p95_ms"], row["requests"]), reverse=True)
    return rows[:20]


async def _database_pressure() -> tuple[dict[str, Any], float]:
    from database import db_manager
    from services.ops_database import get_mysql_status

    pools = []
    highest = 0.0
    for name, pool in db_manager._pools.items():
        size = int(getattr(pool, "size", 0) or 0)
        free = int(getattr(pool, "freesize", 0) or 0)
        maxsize = int(getattr(pool, "maxsize", 0) or 0)
        used = max(0, size - free)
        pressure = used / maxsize if maxsize else 0.0
        highest = max(highest, pressure)
        pools.append({
            "name": name,
            "size": size,
            "used": used,
            "free": free,
            "max_size": maxsize,
            "usage_percent": round(pressure * 100, 1),
        })
    try:
        mysql = await get_mysql_status()
    except Exception as exc:
        mysql = {"connected": False, "error": str(exc)[:160]}
    return {"pools": pools, "mysql": mysql}, highest


async def _background_pressure() -> dict[str, Any]:
    from services.admin_task_queue import build_admin_task_queue

    try:
        queue = await build_admin_task_queue()
    except Exception:
        return {
            "active_count": 0,
            "queued_count": 0,
            "running_count": 0,
            "attention_count": 0,
            "oldest_active_seconds": 0,
            "occupancy_score": 0,
            "categories": [],
            "unavailable_sources": ["后台任务队列"],
        }
    now = datetime.now(timezone.utc)
    categories: dict[str, dict[str, Any]] = {}
    oldest = 0
    for item in queue.get("items", []):
        if not item.get("active"):
            continue
        category = str(item.get("category") or "其他后台任务")
        bucket = categories.setdefault(category, {"category": category, "active": 0, "queued": 0, "running": 0})
        bucket["active"] += 1
        state = str(item.get("state") or "")
        if state in {"queued", "running"}:
            bucket[state] += 1
        raw_started = item.get("started_at") or item.get("created_at")
        if raw_started:
            try:
                started = datetime.fromisoformat(str(raw_started).replace("Z", "+00:00"))
                oldest = max(oldest, int((now - started).total_seconds()))
            except ValueError:
                pass
    active = int(queue.get("active_count", 0))
    queued = int(queue.get("queued_count", 0))
    running = int(queue.get("running_count", 0))
    projection = {}
    report = {}
    try:
        from services.online_projection_jobs import projection_queue_snapshot
        projection = await projection_queue_snapshot()
    except Exception:
        projection = {"unavailable": True}
    try:
        from services.local_report_scheduler import local_report_status
        report = local_report_status()
    except Exception:
        report = {"state": "unavailable"}
    try:
        from services.diagnostics import incident_capture_snapshot
        diagnostic_capture = incident_capture_snapshot()
    except Exception:
        diagnostic_capture = {}
    try:
        from services.runtime_telemetry import snapshot as runtime_telemetry_snapshot
        runtime_telemetry = runtime_telemetry_snapshot()
    except Exception:
        runtime_telemetry = {}
    projection_queued = int(projection.get("queued_count", 0) or 0)
    projection_running = int(projection.get("running_count", 0) or 0)
    projection_failed = int(projection.get("failed_count", 0) or 0)
    projection_oldest = int(projection.get("oldest_wait_seconds", 0) or 0)
    if projection_queued or projection_running or projection_failed:
        categories["派生任务"] = {
            "category": "派生任务",
            "active": projection_queued + projection_running,
            "queued": projection_queued,
            "running": projection_running,
        }
    return {
        "active_count": active + projection_queued + projection_running,
        "queued_count": queued + projection_queued,
        "running_count": running + projection_running,
        "attention_count": int(queue.get("attention_count", 0)) + projection_failed,
        "oldest_active_seconds": max(0, oldest, projection_oldest),
        "occupancy_score": (running + projection_running) * 2 + queued + projection_queued,
        "categories": sorted(categories.values(), key=lambda item: (item["active"], item["running"]), reverse=True),
        "unavailable_sources": queue.get("unavailable_sources", []),
        "online_projection": projection,
        "local_report": report,
        "diagnostic_capture": diagnostic_capture,
        "runtime_telemetry": runtime_telemetry,
    }


async def build_performance_snapshot(minutes: int = 15) -> dict[str, Any]:
    minutes = max(1, min(int(minutes), 60))
    samples = performance_metrics.recent_samples(minutes)
    summary = _aggregate(samples)
    summary.update({
        "requests_per_minute": round(summary["requests"] / minutes, 2),
        "requests_per_second": round(summary["requests"] / (minutes * 60), 2),
        "inflight_current": performance_metrics.inflight,
        "inflight_peak_since_start": performance_metrics.peak_inflight,
    })
    lag_values = performance_metrics.recent_loop_lag(minutes)
    loop_lag = {
        "current_ms": round(lag_values[-1], 1) if lag_values else 0.0,
        "average_ms": round(sum(lag_values) / len(lag_values), 1) if lag_values else 0.0,
        "max_ms": round(max(lag_values), 1) if lag_values else 0.0,
    }
    database, pool_pressure = await _database_pressure()
    background = await _background_pressure()
    mysql = database.get("mysql", {})
    state, signals = performance_metrics.resolve_state(
        summary,
        loop_lag_ms=loop_lag["max_ms"],
        pool_pressure=pool_pressure,
        mysql_threads_running=int(mysql.get("threads_running", 0) or 0),
        mysql_lock_waits=int(mysql.get("lock_waits", 0) or 0),
        background=background,
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "monitoring_started_at": performance_metrics.started_at.isoformat().replace("+00:00", "Z"),
        "window_minutes": minutes,
        "state": state,
        "state_label": {
            "normal": "正常",
            "busy": "繁忙",
            "congested": "拥堵",
            "recovering": "恢复中",
            "warming_up": "采集中",
        }[state],
        "summary": summary,
        "event_loop": loop_lag,
        "signals": signals,
        "timeline": _timeline(samples, minutes),
        "endpoint_groups": _endpoint_rows(samples),
        "database": database,
        "background": background,
    }


async def run_performance_sampler() -> None:
    """Measure event-loop delay without blocking request handling."""
    interval = 1.0
    expected = time.monotonic() + interval
    while True:
        await asyncio.sleep(max(0.0, expected - time.monotonic()))
        observed = time.monotonic()
        performance_metrics.observe_loop_lag((observed - expected) * 1000)
        expected = observed + interval
