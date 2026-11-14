"""Combined read-only health information for the operations center."""

import asyncio
import os
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from config import settings
from database import db_manager
from services.backups import get_backup_schedule
from services.business_time import (
    current_business_date,
    get_business_timezone_name,
    resolve_timezone,
)
from services.ops_client import get_container_overview
from services.ops_database import get_database_overview, get_mysql_status
from services.ops_redaction import redact_text
from services.local_source import local_data_source_enabled


SYNC_DAILY_WINDOW_DAYS = 14


def build_daily_sync_counts(
    rows: list[tuple],
    *,
    now_utc: datetime,
    timezone_name: str,
    window_days: int = SYNC_DAILY_WINDOW_DAYS,
) -> list[dict]:
    """按系统业务时区汇总同步任务，避免用服务器 UTC 日期误分日。"""
    timezone_info = resolve_timezone(timezone_name)
    aware_now = (
        now_utc.replace(tzinfo=timezone.utc)
        if now_utc.tzinfo is None
        else now_utc.astimezone(timezone.utc)
    )
    today = current_business_date(timezone_name, now=aware_now)
    dates = [today - timedelta(days=offset) for offset in range(max(window_days, 1))]
    buckets = {
        day: {
            "business_date": day.isoformat(),
            "total": 0,
            "success": 0,
            "partial": 0,
            "failed": 0,
            "unfinished": 0,
            "manual": 0,
            "scheduled": 0,
        }
        for day in dates
    }
    for row in rows:
        status = str(row[0] or "pending")
        trigger_source = str(row[1] or "manual")
        occurred_at = row[2] or row[3]
        if occurred_at is None:
            continue
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        business_day = occurred_at.astimezone(timezone_info).date()
        bucket = buckets.get(business_day)
        if bucket is None:
            continue
        bucket["total"] += 1
        if status in {"success", "completed"}:
            bucket["success"] += 1
        elif status == "partial":
            bucket["partial"] += 1
        elif status == "failed":
            bucket["failed"] += 1
        else:
            bucket["unfinished"] += 1
        if trigger_source == "scheduled":
            bucket["scheduled"] += 1
        else:
            bucket["manual"] += 1
    return [buckets[day] for day in dates]


def build_daily_txdocs_usage(
    rows: list[tuple],
    *,
    now_utc: datetime,
    timezone_name: str,
    daily_limit: int,
    window_days: int = SYNC_DAILY_WINDOW_DAYS,
) -> dict:
    """Aggregate actual outbound attempts; 400011 overrides local estimates."""
    timezone_info = resolve_timezone(timezone_name)
    aware_now = (
        now_utc.replace(tzinfo=timezone.utc)
        if now_utc.tzinfo is None
        else now_utc.astimezone(timezone.utc)
    )
    today = current_business_date(timezone_name, now=aware_now)
    dates = [today - timedelta(days=offset) for offset in range(max(window_days, 1))]
    buckets = {
        day: {
            "business_date": day.isoformat(),
            "attempts": 0,
            "success": 0,
            "failure": 0,
            "retries": 0,
            "quota_exhausted_responses": 0,
            "estimated_remaining": max(int(daily_limit), 0),
        }
        for day in dates
    }
    today_breakdown: dict[tuple[str, str, str], dict] = {}
    metering_started_at: datetime | None = None
    for row in rows:
        bucket_hour = row[0]
        if bucket_hour is None:
            continue
        if bucket_hour.tzinfo is None:
            bucket_hour = bucket_hour.replace(tzinfo=timezone.utc)
        if metering_started_at is None or bucket_hour < metering_started_at:
            metering_started_at = bucket_hour
        business_day = bucket_hour.astimezone(timezone_info).date()
        bucket = buckets.get(business_day)
        if bucket is None:
            continue
        attempts = int(row[4] or 0)
        success = int(row[5] or 0)
        failure = int(row[6] or 0)
        retries = int(row[7] or 0)
        exhausted = int(row[8] or 0)
        bucket["attempts"] += attempts
        bucket["success"] += success
        bucket["failure"] += failure
        bucket["retries"] += retries
        bucket["quota_exhausted_responses"] += exhausted
        if business_day == today:
            source = str(row[1] or "unknown")
            endpoint = str(row[2] or "other")
            method = str(row[3] or "GET")
            breakdown_bucket = today_breakdown.setdefault(
                (source, endpoint, method),
                {
                    "source": source,
                    "endpoint": endpoint,
                    "method": method,
                    "attempts": 0,
                    "success": 0,
                    "failure": 0,
                    "retries": 0,
                },
            )
            breakdown_bucket["attempts"] += attempts
            breakdown_bucket["success"] += success
            breakdown_bucket["failure"] += failure
            breakdown_bucket["retries"] += retries
    for bucket in buckets.values():
        bucket["estimated_remaining"] = (
            0
            if bucket["quota_exhausted_responses"] > 0
            else max(int(daily_limit) - bucket["attempts"], 0)
        )
    daily = [buckets[day] for day in dates]
    metering_business_day = (
        metering_started_at.astimezone(timezone_info).date()
        if metering_started_at is not None
        else None
    )
    return {
        "daily_limit": int(daily_limit),
        "timezone": timezone_name,
        "today": daily[0],
        "daily": daily,
        "metering_started_at": (
            metering_started_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            if metering_started_at is not None
            else None
        ),
        "today_coverage_complete": bool(
            metering_business_day is not None
            and metering_business_day < today
        ),
        "today_breakdown": sorted(
            today_breakdown.values(),
            key=lambda item: (
                -item["attempts"],
                item["source"],
                item["endpoint"],
            ),
        ),
    }


def _container_component(containers: list[dict], source: str, label: str) -> dict:
    container = next((item for item in containers if item.get("source") == source), None)
    if not container:
        return {"id": source, "label": label, "status": "unavailable", "summary": "容器状态无法读取"}
    status = str(container.get("health") or container.get("status") or "unknown")
    normalized = "healthy" if status in {"healthy", "running"} else "unavailable" if status in {"unavailable", "unknown"} else "warning"
    details = {
        "container_status": status,
        "restart_count": int(container.get("restart_count") or 0),
    }
    if container.get("error"):
        details["error"] = redact_text(str(container["error"]))[:120]
    summary = "容器运行中" if normalized == "healthy" else "容器状态异常" if normalized == "warning" else "容器状态不可用"
    return {"id": source, "label": label, "status": normalized, "summary": summary, "details": details}


async def build_task_pipeline_snapshot(containers: list[dict], mysql_status: dict) -> dict:
    """Collect safe, aggregate-only status for the Production task event path."""
    environment = settings.APP_ENVIRONMENT
    outbox = {"available": False, "domains": []}
    outbox_status = "unavailable"
    try:
        domain_rows: dict[str, dict] = {}
        missing_database = False
        for database in ("online_data", "platform"):
            try:
                pool = db_manager.get_pool(database)
            except ValueError:
                missing_database = True
                break
            conn = await asyncio.wait_for(pool.acquire(), timeout=2)
            try:
                async with conn.cursor() as cur:
                    await asyncio.wait_for(cur.execute(
                        "SELECT domain, status, COUNT(*), "
                        "COALESCE(MAX(CASE WHEN status IN ('pending','retry','publishing') "
                        "THEN TIMESTAMPDIFF(SECOND, occurred_at, UTC_TIMESTAMP()) ELSE 0 END), 0) "
                        "FROM _domain_event_outbox "
                        "WHERE status IN ('pending','publishing','retry','blocked','dead_letter') "
                        "OR (status='published' AND published_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY) "
                        "GROUP BY domain, status"
                    ), timeout=3)
                    rows = await asyncio.wait_for(cur.fetchall(), timeout=3)
                for domain_value, status_value, count_value, oldest_value in rows:
                    domain_value = str(domain_value or "")
                    status = str(status_value or "")
                    if status not in {
                        "pending", "publishing", "published", "retry", "blocked", "dead_letter"
                    }:
                        continue
                    domain = domain_value if re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", domain_value) else "other"
                    domain_key = f"{database}:{domain}"
                    item = domain_rows.setdefault(domain_key, {
                        "domain": domain_key, "statuses": {}, "pending": 0,
                        "oldest_pending_seconds": 0,
                    })
                    count = int(count_value or 0)
                    item["statuses"][status] = item["statuses"].get(status, 0) + count
                    if status in {"pending", "retry", "publishing"}:
                        item["pending"] += count
                        item["oldest_pending_seconds"] = max(
                            item["oldest_pending_seconds"], int(oldest_value or 0)
                        )
            finally:
                pool.release(conn)
        if missing_database:
            raise RuntimeError("required outbox database unavailable")
        outbox = {"available": True, "domains": sorted(domain_rows.values(), key=lambda item: item["domain"])}
        pending = sum(item["pending"] for item in outbox["domains"])
        blocked = sum(
            item["statuses"].get("blocked", 0) + item["statuses"].get("dead_letter", 0)
            for item in outbox["domains"]
        )
        oldest = max((item["oldest_pending_seconds"] for item in outbox["domains"]), default=0)
        outbox_status = "unavailable" if not mysql_status.get("connected") else (
            "warning" if blocked or pending >= 500 or oldest >= 30 else "healthy"
        )
    except Exception:
        pass

    try:
        import redis.asyncio as redis
        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            await asyncio.wait_for(client.ping(), timeout=2)
            redis_reachable = True
        finally:
            await client.aclose()
    except Exception:
        redis_reachable = False

    redis_component = _container_component(containers, "redis", "Redis")
    redis_component["status"] = "healthy" if redis_reachable and redis_component["status"] == "healthy" else "unavailable"
    redis_component["summary"] = "Redis 连通" if redis_component["status"] == "healthy" else "Redis 容器或连接不可用"
    redis_component.setdefault("details", {})["ping"] = redis_reachable

    relay = _container_component(containers, "event-relay", "实时事件中继 (Redis)")
    relay_component = relay
    relay_component["summary"] = "中继进程运行中" if relay["status"] == "healthy" else relay["summary"]

    read_model_enabled = bool(settings.TASK_READ_MODEL_ENABLED)
    pending_total = sum(item["pending"] for item in outbox["domains"])
    oldest_pending = max((item["oldest_pending_seconds"] for item in outbox["domains"]), default=0)
    blocked_total = sum(
        item["statuses"].get("blocked", 0) + item["statuses"].get("dead_letter", 0)
        for item in outbox["domains"]
    )
    outbox_component = {
        "id": "outbox", "label": "事务 Outbox", "status": outbox_status,
        "summary": "按业务域统计待发布事件" if outbox["available"] else "Outbox 状态无法读取",
        "details": {
            "pending": pending_total,
            "blocked_or_dead_letter": blocked_total,
            "oldest_pending_seconds": oldest_pending,
        },
    }
    components = [
        {
            "id": "mysql-authority", "label": "MySQL 权威写入",
            "status": "healthy" if mysql_status.get("connected") else "unavailable",
            "summary": "业务事务与 Outbox 权威存储",
        },
        outbox_component,
        redis_component,
        relay_component,
        {
            "id": "kafka", "label": "Kafka", "status": "not_deployed",
            "summary": "当前 Production 未部署 Kafka",
        },
        {
            "id": "flink", "label": "Flink", "status": "not_deployed",
            "summary": "当前 Production 未部署 Flink",
        },
        {
            "id": "task-read-model", "label": "指令核查 Redis 读模型",
            "status": "unknown" if read_model_enabled else "disabled",
            "summary": "配置已启用，但本面板未验证读模型水位" if read_model_enabled else "Production 配置关闭，MySQL 仍是页面读取路径",
            "details": {"enabled": read_model_enabled},
        },
    ]
    return {"environment": environment, "components": components, "outbox": outbox}


async def build_operations_overview() -> dict:
    try:
        container_data = await get_container_overview()
        containers = container_data.get("containers", [])
        container_error = None
    except Exception as exc:
        containers = []
        container_error = redact_text(str(exc))[:200]

    try:
        mysql_status = await get_mysql_status()
        databases = await get_database_overview()
    except Exception as exc:
        mysql_status = {
            "connected": False,
            "error": redact_text(str(exc))[:200],
        }
        databases = []

    try:
        backup_dir = Path(settings.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        disk_stat = os.statvfs(backup_dir)
        disk_total = disk_stat.f_frsize * disk_stat.f_blocks
        disk_free = disk_stat.f_frsize * disk_stat.f_bavail
        disk = {
            "total_bytes": disk_total,
            "free_bytes": disk_free,
            "used_bytes": disk_total - disk_free,
            "free_percent": round(
                disk_free / disk_total * 100,
                1,
            )
            if disk_total
            else 0,
        }
    except OSError as exc:
        disk = {
            "total_bytes": 0,
            "free_bytes": 0,
            "used_bytes": 0,
            "free_percent": 0,
            "error": redact_text(str(exc))[:200],
        }

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            timezone_name = await get_business_timezone_name(cur)
            await cur.execute("SELECT UTC_TIMESTAMP()")
            server_time = (await cur.fetchone())[0]
            business_today = current_business_date(
                timezone_name,
                now=server_time.replace(tzinfo=timezone.utc),
            )
            first_business_day = business_today - timedelta(
                days=SYNC_DAILY_WINDOW_DAYS - 1,
            )
            first_day_utc = datetime.combine(
                first_business_day,
                time.min,
                tzinfo=resolve_timezone(timezone_name),
            ).astimezone(timezone.utc).replace(tzinfo=None)
            if local_data_source_enabled():
                sync_row = None
                sync_rows = []
                txdocs_usage_rows = []
            else:
                await cur.execute(
                    """
                    SELECT id, status, trigger_source, finished_at
                    FROM _sync_log ORDER BY id DESC LIMIT 1
                    """
                )
                sync_row = await cur.fetchone()
                await cur.execute(
                    """
                    SELECT status, trigger_source, started_at, finished_at
                    FROM _sync_log
                    WHERE COALESCE(started_at, finished_at) >= %s
                    ORDER BY COALESCE(started_at, finished_at) DESC
                    """,
                    (first_day_utc,),
                )
                sync_rows = await cur.fetchall()
                await cur.execute(
                    """
                    SELECT bucket_hour, request_source, endpoint, method,
                           attempt_count, success_count, failure_count,
                           retry_count, quota_exhausted_count
                    FROM _txdocs_api_usage_hourly
                    WHERE bucket_hour >= %s
                    ORDER BY bucket_hour DESC
                    """,
                    (first_day_utc,),
                )
                txdocs_usage_rows = await cur.fetchall()
            await cur.execute(
                """
                SELECT id, status, finished_at, size_bytes
                FROM _backup_jobs ORDER BY id DESC LIMIT 1
                """
            )
            backup_row = await cur.fetchone()
            if local_data_source_enabled():
                oauth_row = None
            else:
                await cur.execute(
                    """
                    SELECT expires_at
                    FROM _config_oauth_tokens ORDER BY id DESC LIMIT 1
                    """
                )
                oauth_row = await cur.fetchone()
    finally:
        pool.release(conn)

    def iso(value):
        return value.isoformat() + "Z" if value else None

    sync_daily_counts = build_daily_sync_counts(
        sync_rows,
        now_utc=server_time,
        timezone_name=timezone_name,
    )
    txdocs_request_usage = build_daily_txdocs_usage(
        txdocs_usage_rows,
        now_utc=server_time,
        timezone_name=timezone_name,
        daily_limit=settings.TXDOCS_DAILY_REQUEST_LIMIT,
    )
    oauth = {"configured": bool(oauth_row), "status": "not_configured"}
    if oauth_row:
        expires_at = oauth_row[0]
        if expires_at is None:
            oauth_status = "unknown"
        else:
            remaining = (expires_at - server_time).total_seconds()
            if remaining <= 0:
                oauth_status = "expired"
            elif remaining <= 7 * 24 * 60 * 60:
                oauth_status = "expiring"
            else:
                oauth_status = "healthy"
        oauth.update(
            {
                "status": oauth_status,
                "expires_at": iso(expires_at),
            }
        )

    task_pipeline = await build_task_pipeline_snapshot(containers, mysql_status)
    return {
        "server_time": iso(server_time),
        "containers": containers,
        "container_error": container_error,
        "disk": disk,
        "mysql": mysql_status,
        "task_pipeline": task_pipeline,
        "databases": databases,
        "latest_sync": {
            "id": sync_row[0],
            "status": sync_row[1],
            "trigger_source": sync_row[2],
            "finished_at": iso(sync_row[3]),
        }
        if sync_row
        else None,
        "sync_timezone": timezone_name,
        "sync_daily_counts": sync_daily_counts,
        "txdocs_request_usage": txdocs_request_usage,
        "latest_backup": {
            "id": backup_row[0],
            "status": backup_row[1],
            "finished_at": iso(backup_row[2]),
            "size_bytes": backup_row[3],
        }
        if backup_row
        else None,
        "backup_schedule": await get_backup_schedule(),
        "oauth": oauth,
    }
