"""统一外部来源目录、计划配置和运行安全合同。

外部来源的正文、凭据和业务字段不进入本模块；这里仅保存来源标识、运行
状态、Cron 配置以及脱敏门禁结果。真正的来源读取仍由各自的 service 负责。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from croniter import croniter
except ImportError:  # pragma: no cover - dependency is installed in deployments
    croniter = None

from services.business_time import DEFAULT_TIMEZONE, resolve_timezone
from services.maintenance import is_super_admin_user


_SCHEDULE_MIGRATION_KEY = "external_acquisition_schedule_migrated_v1"


@dataclass(frozen=True)
class ExternalSourceDefinition:
    key: str
    name: str
    category: str
    description: str
    permission: str | None
    supports_manual: bool = True
    supports_schedule: bool = True
    supports_auto_commit: bool = False
    params_schema: str = "none"


SOURCE_DEFINITIONS: tuple[ExternalSourceDefinition, ...] = (
    ExternalSourceDefinition(
        "registry_certificate", "责任告知书", "外部读取",
        "读取责任告知书来源快照并保留分页进度。", "registry.import.manage",
        supports_auto_commit=True, params_schema="certificate",
    ),
    ExternalSourceDefinition(
        "residence_full_scan", "居住证全量查询", "外部读取",
        "按已配置社区账号执行居住证平台只读查询。", None,
        supports_auto_commit=True, params_schema="residence",
    ),
    ExternalSourceDefinition(
        "qmf_source", "全民防来源同步", "外部读取",
        "读取全民防模型三未核查来源并更新本地来源投影。", "sync.trigger",
        supports_auto_commit=True, params_schema="qmf",
    ),
    ExternalSourceDefinition(
        "qmf_status_scan", "全民防反馈核对", "自动任务",
        "核对已完成模型三任务的外部反馈状态。", "qmf.registration.execute",
        supports_auto_commit=True, params_schema="qmf_scan",
    ),
    ExternalSourceDefinition(
        "visit_source", "走访与星级来源", "外部读取",
        "读取走访明细和星级来源；缺页、冲突或不完整时保留旧快照。",
        "visit.source.manage", supports_auto_commit=True, params_schema="date_range",
    ),
    ExternalSourceDefinition(
        "code_summary", "平安码与管家码", "外部读取",
        "读取指定业务日期的平安码和管家码汇总。", "visit.source.manage",
        supports_auto_commit=True, params_schema="date_range",
    ),
    ExternalSourceDefinition(
        "venue_cloud", "场所云端报备", "外部读取",
        "拉取场所云端报备并保留拒绝、待重试和不确定状态。", "venue.manage",
        supports_auto_commit=True, params_schema="business_date",
    ),
    ExternalSourceDefinition(
        "txdocs_monitor", "腾讯只读监控", "监控",
        "展示生产只读监控状态，不进入业务导入或写回链路。", None,
        supports_manual=False, supports_schedule=False, supports_auto_commit=False,
        params_schema="monitor",
    ),
)

SOURCE_MAP = {item.key: item for item in SOURCE_DEFINITIONS}
SOURCE_ALIASES = {
    "code_summary_fetch": "code_summary",
    "visit_source_preview": "visit_source",
    "visit_source_confirm": "visit_source",
}

_SAFE_PARAM_KEYS = {
    "start_date", "end_date", "business_date", "mode", "full_scan",
    "source", "interval_minutes", "include_rating",
}
_SENSITIVE_PARAM_MARKERS = ("password", "token", "secret", "cookie", "authorization", "credential")


def source_definition(source_key: str) -> ExternalSourceDefinition:
    source_key = SOURCE_ALIASES.get(source_key, source_key)
    try:
        return SOURCE_MAP[source_key]
    except KeyError as exc:
        raise ValueError("未知外部来源") from exc


def sanitize_source_params(source_key: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """只允许可审计的调度参数，阻止凭据和业务正文进入计划/运行表。"""
    source_definition(source_key)
    clean: dict[str, Any] = {}
    for raw_key, value in (params or {}).items():
        key = str(raw_key).strip()
        lowered = key.lower()
        if any(marker in lowered for marker in _SENSITIVE_PARAM_MARKERS):
            raise ValueError("调度参数不能包含凭据或会话字段")
        if key not in _SAFE_PARAM_KEYS:
            raise ValueError(f"不支持的来源参数：{key}")
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
        elif isinstance(value, list) and len(value) <= 100 and all(isinstance(item, (str, int)) for item in value):
            clean[key] = value
        else:
            raise ValueError(f"来源参数格式无效：{key}")
    return clean


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _utc_text(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def validate_timezone(value: str | None) -> str:
    candidate = (value or DEFAULT_TIMEZONE).strip()
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("无效的时区")
    return candidate


def _fallback_cron_next(expr: str, base: datetime) -> datetime:
    """只在本地没有安装 croniter 时支持常用五字段，部署仍强制使用 croniter。"""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("Cron 必须是标准五字段表达式")
    minute, hour, day, month, weekday = fields
    if day != "*" or month != "*" or weekday != "*":
        raise ValueError("当前环境缺少 croniter，复杂 Cron 暂不可校验")
    step = 1
    if minute.startswith("*/") and minute[2:].isdigit():
        step = max(1, int(minute[2:]))
        minute = "*"
    if minute != "*" and not minute.isdigit():
        raise ValueError("无效的分钟字段")
    if hour != "*" and not hour.isdigit():
        raise ValueError("无效的小时字段")
    from datetime import timedelta
    candidate = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        hour_match = hour == "*" or candidate.hour == int(hour)
        minute_match = candidate.minute % step == 0 if minute == "*" else candidate.minute == int(minute)
        if hour_match and minute_match:
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("无法计算下一次运行时间")


def next_run(expr: str, timezone_name: str, now: datetime | None = None) -> datetime:
    expression = " ".join(str(expr or "").split())
    if len(expression.split()) != 5:
        raise ValueError("Cron 必须是标准五字段表达式")
    timezone_name = validate_timezone(timezone_name)
    local_now = (now or datetime.now(timezone.utc)).astimezone(resolve_timezone(timezone_name))
    try:
        value = croniter(expression, local_now).get_next(datetime) if croniter else _fallback_cron_next(expression, local_now)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("无效的 Cron 表达式") from exc
    return value.astimezone(timezone.utc).replace(tzinfo=None)


async def ensure_external_acquisition_schema(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _external_acquisition_schedules (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            source_key VARCHAR(64) NOT NULL,
            enabled TINYINT(1) NOT NULL DEFAULT 0,
            cron_expr VARCHAR(120) NOT NULL DEFAULT '0 2 * * *',
            timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
            params_json JSON NOT NULL,
            next_run_at DATETIME DEFAULT NULL,
            last_run_at DATETIME DEFAULT NULL,
            last_status VARCHAR(20) DEFAULT NULL,
            last_error_code VARCHAR(80) DEFAULT NULL,
            revision BIGINT NOT NULL DEFAULT 1,
            updated_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_external_schedule_source (source_key),
            INDEX idx_external_schedule_due (enabled, next_run_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    columns = {
        "schedule_id": "BIGINT DEFAULT NULL",
        "trigger_source": "VARCHAR(20) NOT NULL DEFAULT 'manual'",
        "auto_commit_status": "VARCHAR(30) DEFAULT NULL",
        "safety_gate_code": "VARCHAR(80) DEFAULT NULL",
    }
    for column, definition in columns.items():
        await cur.execute("SHOW COLUMNS FROM _external_acquisition_runs LIKE %s", (column,))
        if not await cur.fetchone():
            await cur.execute(f"ALTER TABLE _external_acquisition_runs ADD COLUMN `{column}` {definition}")
    for definition in SOURCE_DEFINITIONS:
        await cur.execute(
            "INSERT IGNORE INTO _external_acquisition_schedules "
            "(source_key,enabled,cron_expr,timezone,params_json) VALUES (%s,0,'0 2 * * *','Asia/Shanghai',%s)",
            (definition.key, _json({})),
        )

    # Preserve explicitly enabled legacy schedules exactly once.  The marker
    # prevents a later restart from re-enabling a schedule that an admin has
    # deliberately disabled in the unified workbench.
    await cur.execute(
        "SELECT config_value FROM _system_config WHERE config_key=%s",
        (_SCHEDULE_MIGRATION_KEY,),
    )
    migrated = bool(await cur.fetchone())
    if not migrated:
        from config import settings

        def _bool(value: Any) -> bool:
            return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

        def _cron_time(value: Any, fallback: str = "0 2 * * *") -> str:
            text = str(value or "").strip()
            try:
                hour, minute = (int(part) for part in text.split(":", 1))
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return f"{minute} {hour} * * *"
            except (TypeError, ValueError):
                pass
            return fallback

        def _cron_interval(value: Any) -> str:
            try:
                minutes = max(5, min(59, int(value)))
            except (TypeError, ValueError):
                minutes = 30
            return f"*/{minutes} * * * *"

        await cur.execute(
            "SELECT config_key,config_value FROM _system_config "
            "WHERE config_key IN ('residence_lookup_enabled',"
            "'residence_full_scan_interval_minutes','qmf_status_scan_enabled',"
            "'qmf_status_scan_time')"
        )
        legacy = {str(row[0]): row[1] for row in await cur.fetchall()}
        migrations = {
            "registry_certificate": (
                bool(getattr(settings, "CERTIFICATE_SOURCE_DAILY_ENABLED", False)),
                _cron_time(getattr(settings, "CERTIFICATE_SOURCE_DAILY_TIME", "02:30")),
                {},
            ),
            "residence_full_scan": (
                _bool(legacy.get("residence_lookup_enabled", False)),
                _cron_interval(legacy.get("residence_full_scan_interval_minutes", 30)),
                {},
            ),
            "qmf_status_scan": (
                _bool(legacy.get("qmf_status_scan_enabled", getattr(settings, "QMF_STATUS_SCAN_ENABLED", False))),
                _cron_time(legacy.get("qmf_status_scan_time", getattr(settings, "QMF_STATUS_SCAN_TIME", ""))),
                {},
            ),
            "venue_cloud": (
                bool(getattr(settings, "VENUE_CLOUD_PULL_ENABLED", False)),
                _cron_interval(getattr(settings, "VENUE_CLOUD_PULL_INTERVAL_SECONDS", 300) // 60),
                {},
            ),
        }
        for source_key, (enabled, cron_expr, params) in migrations.items():
            await cur.execute(
                "UPDATE _external_acquisition_schedules SET enabled=%s,cron_expr=%s,"
                "params_json=%s WHERE source_key=%s AND revision=1",
                (1 if enabled else 0, cron_expr, _json(params), source_key),
            )
            if enabled:
                # Compute the first due time during migration; the scheduler
                # only claims rows with a populated next_run_at.
                await cur.execute(
                    "UPDATE _external_acquisition_schedules SET next_run_at=%s "
                    "WHERE source_key=%s",
                    (next_run(cron_expr, DEFAULT_TIMEZONE), source_key),
                )
        await cur.execute(
            "INSERT IGNORE INTO _system_config (config_key,config_value) VALUES (%s,'1')",
            (_SCHEDULE_MIGRATION_KEY,),
        )


def _public_schedule(row: tuple | None) -> dict[str, Any] | None:
    if not row:
        return None
    raw = row[5]
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        params = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        params = {}
    if isinstance(params, dict):
        params = {key: value for key, value in params.items() if str(key) in _SAFE_PARAM_KEYS}
    else:
        params = {}
    return {
        "id": int(row[0]), "source_key": str(row[1]), "enabled": bool(row[2]),
        "cron_expr": str(row[3]), "timezone": str(row[4]), "params": params,
        "next_run_at": _utc_text(row[6]), "last_run_at": _utc_text(row[7]),
        "last_status": str(row[8]) if row[8] else None,
        "last_error_code": str(row[9]) if row[9] else None,
        "revision": int(row[10]), "updated_by": int(row[11]) if row[11] is not None else None,
        "created_at": _utc_text(row[12]), "updated_at": _utc_text(row[13]),
    }


SCHEDULE_SELECT = """SELECT id,source_key,enabled,cron_expr,timezone,params_json,
    next_run_at,last_run_at,last_status,last_error_code,revision,updated_by,created_at,updated_at
    FROM _external_acquisition_schedules"""


async def list_schedules(conn) -> list[dict[str, Any]]:
    async with conn.cursor() as cur:
        await cur.execute(SCHEDULE_SELECT + " ORDER BY id")
        return [_public_schedule(row) for row in await cur.fetchall()]


async def get_schedule(conn, source_key: str) -> dict[str, Any] | None:
    async with conn.cursor() as cur:
        await cur.execute(SCHEDULE_SELECT + " WHERE source_key=%s", (source_key,))
        return _public_schedule(await cur.fetchone())


def can_access_source(user: dict, definition: ExternalSourceDefinition) -> bool:
    if is_super_admin_user(user):
        return True
    return bool(definition.permission and definition.permission in set(user.get("permissions") or []))


async def update_schedule(conn, source_key: str, *, enabled: bool, cron_expr: str,
                          timezone_name: str, params: dict[str, Any], revision: int,
                          updated_by: int) -> dict[str, Any]:
    definition = source_definition(source_key)
    if not definition.supports_schedule:
        raise ValueError("该来源不支持自动调度")
    timezone_name = validate_timezone(timezone_name)
    params = sanitize_source_params(source_key, params)
    cron_expr = " ".join(str(cron_expr or "").split())
    # Validate even while disabling; otherwise an invalid expression can be
    # stored and fail much later when an administrator re-enables the source.
    candidate_next_at = next_run(cron_expr, timezone_name)
    next_at = candidate_next_at if enabled else None
    async with conn.cursor() as cur:
        await cur.execute(SCHEDULE_SELECT + " WHERE source_key=%s FOR UPDATE", (source_key,))
        current = await cur.fetchone()
        if not current:
            raise LookupError("调度记录不存在")
        if int(current[10]) != int(revision):
            raise RuntimeError("schedule_revision_conflict")
        await cur.execute(
            "UPDATE _external_acquisition_schedules SET enabled=%s,cron_expr=%s,timezone=%s,"
            "params_json=%s,next_run_at=%s,revision=revision+1,updated_by=%s WHERE source_key=%s",
            (1 if enabled else 0, cron_expr, timezone_name, _json(params), next_at, updated_by, source_key),
        )
    await conn.commit()
    return (await get_schedule(conn, source_key)) or {}


async def claim_due_schedule() -> dict[str, Any] | None:
    from database import db_manager

    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    SCHEDULE_SELECT + " WHERE enabled=1 AND ("
                    "(next_run_at IS NOT NULL AND next_run_at<=UTC_TIMESTAMP() "
                    " AND COALESCE(last_status,'')<>'running') OR "
                    "(last_status='running' AND last_run_at IS NOT NULL "
                    " AND last_run_at<DATE_SUB(UTC_TIMESTAMP(), INTERVAL 15 MINUTE) "
                    " AND NOT EXISTS (SELECT 1 FROM _external_acquisition_runs r "
                    "   WHERE r.schedule_id=_external_acquisition_schedules.id "
                    "   AND r.status IN ('queued','running')))) "
                    "ORDER BY COALESCE(next_run_at,last_run_at) LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
                row = await cur.fetchone()
                if not row:
                    await conn.rollback()
                    return None
                upcoming = next_run(str(row[3]), str(row[4]))
                await cur.execute(
                    "UPDATE _external_acquisition_schedules SET last_run_at=UTC_TIMESTAMP(),"
                    "last_status='running',last_error_code=NULL,next_run_at=%s,revision=revision+1 WHERE id=%s",
                    (upcoming, int(row[0])),
                )
                await conn.commit()
                result = _public_schedule(tuple(row))
                result["next_run_at"] = _utc_text(upcoming)
                result["revision"] = int(result["revision"]) + 1
                result["last_status"] = "running"
                return result
        except Exception:
            await conn.rollback()
            raise


async def mark_schedule_result(schedule_id: int, status: str, error_code: str | None = None) -> None:
    from database import db_manager

    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _external_acquisition_schedules SET last_status=%s,last_error_code=%s WHERE id=%s",
                (status[:20], error_code[:80] if error_code else None, schedule_id),
            )
        await conn.commit()

async def notify_schedule_failure(source_key: str, run_id: int, code: str) -> None:
    from database import db_manager

    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT IGNORE INTO _notifications (user_id,category,severity,title,content,related_task_id,created_at) "
                "SELECT id,'external_acquisition','error',%s,%s,%s,UTC_TIMESTAMP() FROM _users WHERE role='super_admin'",
                ("外部来源自动任务未提交", f"来源 {source_key} 的运行 #{run_id} 被安全门禁阻止，错误码：{code}"[:1000], run_id),
            )
        await conn.commit()
