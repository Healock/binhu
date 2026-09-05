"""Safe operator entry point for shadow seed/run/verify/cleanup."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from fixture import make_tasks, write_manifest
from shadow_guard import ShadowContext, ShadowSafetyError, require_shadow_context


ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"
DURATION_RE = re.compile(r"^[1-9][0-9]*(?:s|m|h)$")
IMAGE_DIGEST_RE = re.compile(r"(?:^|@)sha256:[0-9a-fA-F]{64}$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
REQUIRED_PRODUCTION_PROOF_SCOPES = {
    "shadow_source_refs",
    "shadow_usernames",
    "loadtest_prefixes",
    "legacy_shadow_source_kind",
}
REQUIRED_SHADOW_ONLINE_TABLES = {
    "_sessions",
    "_user_presence_clients",
    "_users",
    "_online_source_projection",
    "_online_projection_jobs",
    "_shadow_loadtest_marker",
}
REQUIRED_SHADOW_DAILY_TABLES = {"_daily_report_meta", "_daily_task_ledger"}
REQUIRED_SHADOW_SEEDED_TABLES = {"_shadow_loadtest_expectations"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="滨湖智慧平台影子压测安全工具")
    parser.add_argument("command", choices=("seed", "run", "verify", "cleanup"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", choices=("login", "mixed", "conflict"), default="mixed")
    parser.add_argument("--users", type=int, default=50, choices=(5, 20, 50, 75))
    parser.add_argument("--duration", default="30m")
    parser.add_argument("--production-proof", type=Path)
    parser.add_argument("--event-log", type=Path,
                        help="verify 只核对本阶段事件文件；省略时兼容核对本运行全部事件")
    return parser


def _manifest(run_id: str) -> Path:
    return ARTIFACTS / f"shadow-fixture-{run_id}.json"


def _runtime_index(run_id: str) -> Path:
    return ARTIFACTS / f"shadow-runtime-{run_id}.json"


def _last_stage_index(run_id: str) -> Path:
    return ARTIFACTS / f"{run_id}-last-stage.json"


def _require_database_credentials() -> tuple[str, str]:
    user = os.environ.get("SHADOW_DB_USER", "").strip()
    password = os.environ.get("SHADOW_DB_PASSWORD", "")
    if not user or not password:
        raise ShadowSafetyError("缺少 SHADOW_DB_USER 或 SHADOW_DB_PASSWORD")
    return user, password


def _connect(context: ShadowContext, database: str | None = None):
    try:
        import pymysql
    except ImportError as exc:
        raise ShadowSafetyError("请先安装 load-tests/requirements.txt") from exc
    user, password = _require_database_credentials()
    return pymysql.connect(
        host=context.db_host,
        port=context.db_port,
        user=user,
        password=password,
        database=database or context.db_name,
        connect_timeout=5,
        read_timeout=30,
        write_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _verify_marker(context: ShadowContext, *, allow_placeholder: bool) -> list[str]:
    connection = _connect(context)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW TABLES LIKE '_shadow_loadtest_marker'")
            if not cursor.fetchone():
                raise ShadowSafetyError("影子数据库缺少安全标记表")
            cursor.execute(
                "SELECT run_id FROM _shadow_loadtest_marker WHERE environment='shadow'"
            )
            rows = [str(row["run_id"]) for row in cursor.fetchall()]
    finally:
        connection.close()
    allowed = {context.run_id}
    if allow_placeholder:
        allowed.add("__UNSEEDED__")
    if not rows or any(row not in allowed for row in rows):
        raise ShadowSafetyError(f"影子数据库标记不属于本次运行：{rows}")
    if context.run_id not in rows and "__UNSEEDED__" not in rows:
        raise ShadowSafetyError("影子数据库标记与运行编号不一致")
    return rows


def _verify_tables(context: ShadowContext, database: str, required: set[str]) -> list[str]:
    connection = _connect(context, database)
    try:
        with connection.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(required))
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN (" + placeholders + ")",
                (database, *sorted(required)),
            )
            present = {str(row["TABLE_NAME"]) for row in cursor.fetchall()}
    finally:
        connection.close()
    return sorted(required - present)


def _verify_shadow_schema(context: ShadowContext, *, seeded: bool = False) -> None:
    """Fail closed before seed or traffic if either isolated schema is partial.

    The expectations table is created by seed itself, so it is intentionally
    checked only for run/verify.  Daily-report metadata belongs to the
    separate run-scoped daily database, never the online database.
    """
    daily_database = os.environ.get("SHADOW_DAILY_DB_NAME", "").strip()
    if not daily_database or not re.fullmatch(r"LoadTest_[A-Za-z0-9_]+_daily", daily_database):
        raise ShadowSafetyError("缺少有效的 SHADOW_DAILY_DB_NAME")
    missing_online = _verify_tables(context, context.db_name, REQUIRED_SHADOW_ONLINE_TABLES)
    missing_daily = _verify_tables(context, daily_database, REQUIRED_SHADOW_DAILY_TABLES)
    missing_seeded = (
        _verify_tables(context, context.db_name, REQUIRED_SHADOW_SEEDED_TABLES)
        if seeded else []
    )
    details = [
        *(f"在线库.{item}" for item in missing_online),
        *(f"日报库.{item}" for item in missing_daily),
        *(f"在线库.{item}" for item in missing_seeded),
    ]
    if details:
        raise ShadowSafetyError("影子环境 schema 不完整，缺少必需表：" + ", ".join(details))


def _verify_projection_performance_schema(context: ShadowContext) -> None:
    connection = _connect(context)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='_online_projection_jobs' "
                "AND column_name='available_at'",
                (context.db_name,),
            )
            available_at = bool(cursor.fetchone())
            cursor.execute(
                "SELECT index_name,GROUP_CONCAT(column_name ORDER BY seq_in_index) AS columns_list "
                "FROM information_schema.statistics WHERE table_schema=%s "
                "AND table_name IN ('_online_projection_jobs','_online_source_rows') "
                "GROUP BY table_name,index_name",
                (context.db_name,),
            )
            indexes = _projection_index_map(cursor.fetchall())
    finally:
        connection.close()
    if not available_at:
        raise ShadowSafetyError("影子环境 schema 不完整：派生队列缺少 available_at")
    expected = {
        "idx_projection_job_available": "status,available_at,created_at,id",
        "idx_online_source_ref": "source_kind,source_ref",
    }
    missing = [name for name, columns in expected.items() if indexes.get(name) != columns]
    if missing:
        raise ShadowSafetyError("影子环境缺少 0.28.8 性能索引：" + ", ".join(missing))


def _projection_index_map(rows: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """Normalize information_schema metadata returned by different MySQL cursors.

    DictCursor implementations may preserve aliases as lowercase names or expose
    them in uppercase (for example ``INDEX_NAME``/``COLUMNS_LIST``).  The shadow
    preflight must accept both forms without weakening the exact index checks.
    """
    indexes: dict[str, str] = {}
    for row in rows:
        index_name = row.get("index_name", row.get("INDEX_NAME"))
        columns = row.get("columns_list", row.get("COLUMNS_LIST"))
        if index_name is None:
            continue
        indexes[str(index_name)] = str(columns or "")
    return indexes


def _docker() -> str:
    command = shutil.which("docker")
    if not command:
        raise ShadowSafetyError("未找到 Docker；影子环境只能通过固定 Compose 执行")
    return command


def _verify_pinned_images() -> None:
    for key in ("SHADOW_BACKEND_IMAGE", "SHADOW_MYSQL_IMAGE", "SHADOW_REDIS_IMAGE"):
        value = os.environ.get(key, "").strip()
        if not IMAGE_DIGEST_RE.search(value):
            raise ShadowSafetyError(f"{key} 必须固定为精确 sha256 镜像 ID 或 digest")


def _compose_command(context: ShadowContext, *args: str) -> list[str]:
    compose_file = ROOT / "docker-compose.shadow.yml"
    if not compose_file.is_file():
        raise ShadowSafetyError("找不到影子 Compose 文件")
    return [
        _docker(), "compose", "-p", context.project,
        "-f", str(compose_file), *args,
    ]


def _write_runtime_index(context: ShadowContext) -> dict[str, int]:
    connection = _connect(context)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT expectation.ordinal_no,expectation.parser_type,expectation.row_key,"
                "expectation.source_id,expectation.initial_revision,expectation.scenario,"
                "expectation.property_id,expectation.property_version,projection.community,"
                "projection.inspector FROM _shadow_loadtest_expectations expectation "
                "JOIN _online_source_projection projection "
                "ON projection.parser_type COLLATE utf8mb4_unicode_ci="
                "expectation.parser_type COLLATE utf8mb4_unicode_ci "
                "AND projection.row_key COLLATE utf8mb4_unicode_ci="
                "expectation.row_key COLLATE utf8mb4_unicode_ci WHERE expectation.run_id=%s "
                "ORDER BY expectation.ordinal_no",
                (context.run_id,),
            )
            rows = cursor.fetchall()
            cursor.execute(
                "SELECT id,current_version,community_name_snapshot FROM registry_properties "
                "WHERE source_type='shadow_loadtest' AND source_ref LIKE %s ORDER BY id",
                (f"shadow:{context.run_id}:property:%",),
            )
            properties_by_community: dict[str, list[dict[str, int]]] = defaultdict(list)
            for property_row in cursor.fetchall():
                properties_by_community[str(property_row["community_name_snapshot"] or "")].append({
                    "property_id": int(property_row["id"]),
                    "property_version": int(property_row["current_version"]),
                })
    finally:
        connection.close()
    payload = {
        "schema_version": 1,
        "run_id": context.run_id,
        "fictional_only": True,
        "tasks": [
            {
                "ordinal": int(row["ordinal_no"]),
                "parser_type": str(row["parser_type"]),
                "row_key": str(row["row_key"]),
                "source_id": int(row["source_id"]),
                "initial_revision": int(row["initial_revision"]),
                "scenario": str(row["scenario"]),
                "property_id": int(row["property_id"]) if row["property_id"] is not None else None,
                "property_version": int(row["property_version"]) if row["property_version"] is not None else None,
                "community": str(row["community"] or ""),
                "inspector": str(row["inspector"] or ""),
                "property_candidates": properties_by_community.get(
                    str(row["community"] or ""), []
                ),
            }
            for row in rows
        ],
    }
    invalid_candidate_counts = sorted({
        (item["community"], len(item["property_candidates"]))
        for item in payload["tasks"]
        if item["community"] and len(item["property_candidates"]) != 4
    })
    if invalid_candidate_counts:
        raise ShadowSafetyError(
            f"每个任务社区应有 4 套候选房屋：{invalid_candidate_counts}"
        )
    path = _runtime_index(context.run_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = {"runtime_tasks": len(rows)}
    if counts["runtime_tasks"] != 3600:
        raise ShadowSafetyError(f"运行索引应有 3600 条任务，实际为 {len(rows)}")
    return counts


def seed(run_id: str) -> int:
    context = require_shadow_context(run_id)
    _verify_pinned_images()
    _verify_shadow_schema(context)
    _verify_marker(context, allow_placeholder=True)
    migration = subprocess.run(
        _compose_command(
            context, "exec", "-T", "backend", "python", "-m",
            "migrations.online_projection_queue_performance", "migrate", "--apply",
        ),
        cwd=ROOT,
        check=False,
    )
    if migration.returncode:
        return migration.returncode
    _verify_projection_performance_schema(context)
    path = write_manifest(ARTIFACTS, context.run_id)
    command = _compose_command(
        context, "exec", "-T", "backend", "python",
        "/load-tests/seed_shadow.py", "--run-id", context.run_id,
    )
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        return result.returncode
    _verify_marker(context, allow_placeholder=False)
    counts = _write_runtime_index(context)
    print(json.dumps({
        "run_id": context.run_id,
        "manifest": str(path),
        "runtime_index": str(_runtime_index(context.run_id)),
        "fictional_only": True,
        **counts,
    }, ensure_ascii=False))
    return 0


def _validate_https_origin(value: str, *, label: str) -> str:
    parsed = urlparse(value.strip().rstrip("/"))
    if (
        parsed.scheme != "https" or not parsed.netloc or parsed.username
        or parsed.password or parsed.path not in {"", "/"}
        or parsed.query or parsed.fragment
    ):
        raise ShadowSafetyError(f"{label} 必须是无路径、无凭据的 HTTPS origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_production_health_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme != "https" or not parsed.netloc or parsed.username
        or parsed.password or parsed.path != "/api/health"
        or parsed.params or parsed.query or parsed.fragment
    ):
        raise ShadowSafetyError(
            "PRODUCTION_HEALTH_URL 必须是可信正式域名下的精确 HTTPS /api/health 地址"
        )
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _validate_production_containers(names: tuple[str, ...]) -> tuple[str, ...]:
    if not names:
        raise ShadowSafetyError("50/75 人运行必须设置 PRODUCTION_CONTAINER_NAMES")
    if len(set(names)) != len(names) or any(not CONTAINER_RE.fullmatch(name) for name in names):
        raise ShadowSafetyError("PRODUCTION_CONTAINER_NAMES 包含重复或非法容器名")
    docker = _docker()
    for name in names:
        result = subprocess.run(
            [docker, "inspect", "--type", "container", name],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise ShadowSafetyError(f"无法读取正式容器状态：{name}")
    return names


def _validate_run_shape(scenario: str, users: int, duration: str) -> None:
    if not DURATION_RE.fullmatch(duration):
        raise ShadowSafetyError("duration 仅接受 30s、5m、1h 这类正整数时长")
    expected = {"login": {50}, "mixed": {5, 50, 75}, "conflict": {20}}[scenario]
    if users not in expected:
        raise ShadowSafetyError(f"{scenario} 场景只允许用户数：{sorted(expected)}")


def run(run_id: str, users: int, duration: str, scenario: str) -> int:
    context = require_shadow_context(run_id)
    _verify_pinned_images()
    _verify_shadow_schema(context, seeded=True)
    _verify_projection_performance_schema(context)
    _validate_run_shape(scenario, users, duration)
    _verify_marker(context, allow_placeholder=False)
    manifest = _manifest(context.run_id)
    runtime_index = _runtime_index(context.run_id)
    if not manifest.is_file():
        raise ShadowSafetyError("找不到 seed 生成的影子数据清单")
    if scenario != "login" and not runtime_index.is_file():
        raise ShadowSafetyError("找不到 seed 生成的真实任务运行索引")

    base_url = _validate_https_origin(
        os.environ.get("SHADOW_BASE_URL", ""), label="SHADOW_BASE_URL"
    )
    production_health = os.environ.get("PRODUCTION_HEALTH_URL", "").strip()
    production_containers = tuple(
        item.strip()
        for item in os.environ.get("PRODUCTION_CONTAINER_NAMES", "").split(",")
        if item.strip()
    )
    if users >= 50:
        production_health = _validate_production_health_url(production_health)
        production_containers = _validate_production_containers(production_containers)

    locust_command = shutil.which("locust")
    if not locust_command:
        raise ShadowSafetyError("未找到 Locust；请先安装 load-tests/requirements.txt")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    artifact_key = f"{context.run_id}-{scenario}-{users}-{stamp}"
    csv_prefix = ARTIFACTS / artifact_key
    event_log = ARTIFACTS / f"{context.run_id}-events-{scenario}-{stamp}.jsonl"
    # Login-only scenarios do not emit business write events, but verify still
    # needs an explicit empty stage log so a completed run is distinguishable
    # from a missing or corrupted artifact.
    event_log.touch()
    child_env = os.environ.copy()
    child_env["LOAD_TEST_SCENARIO"] = scenario
    child_env["SHADOW_RUNTIME_INDEX"] = str(runtime_index)
    child_env["SHADOW_EVENT_LOG"] = str(event_log)
    if users == 75:
        child_env["LOAD_TEST_BURST"] = "1"
    else:
        child_env.pop("LOAD_TEST_BURST", None)

    locust_file = ROOT / ("login_locust.py" if scenario == "login" else "locustfile.py")
    spawn_rate = (
        10 if scenario == "login"
        else 1 if users == 5
        else 20 if users == 20
        else 15 if users == 75
        else 0.17
    )
    command = [
        locust_command, "-f", str(locust_file), "--headless",
        "--users", str(users), "--spawn-rate", str(spawn_rate),
        "--run-time", duration, "--host", base_url,
        "--stop-timeout", "15",
        "--csv", str(csv_prefix), "--csv-full-history",
        "--html", str(ARTIFACTS / f"{artifact_key}.html"),
    ]
    process = subprocess.Popen(command, cwd=ROOT, env=child_env)
    from metrics import MonitorConfig, monitor_process

    db_user, db_password = _require_database_credentials()
    monitor = monitor_process(process, MonitorConfig(
        run_id=context.run_id,
        compose_project=context.project,
        db_host=context.db_host,
        db_port=context.db_port,
        db_user=db_user,
        db_password=db_password,
        db_name=context.db_name,
        locust_prefix=csv_prefix,
        artifact_dir=ARTIFACTS,
        scenario=scenario,
        production_health_url=production_health,
        production_containers=production_containers,
    ))
    _last_stage_index(context.run_id).write_text(json.dumps({
        "run_id": context.run_id,
        "scenario": scenario,
        "users": users,
        "event_log": str(event_log),
        "artifact_key": artifact_key,
        "finished_at": time.time(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "run_id": context.run_id,
        "scenario": scenario,
        "users": users,
        "artifacts": artifact_key,
        "monitor": monitor,
    }, ensure_ascii=False))
    if monitor.get("stopped"):
        return 3
    return int(process.returncode or 0)


def _load_events(run_id: str, event_log: Path | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if event_log is not None:
        resolved = event_log.resolve()
        if resolved.parent != ARTIFACTS.resolve() or not resolved.name.startswith(f"{run_id}-events-"):
            raise ShadowSafetyError("事件日志不属于本次运行 artifacts")
        paths = [resolved]
    else:
        paths = sorted(ARTIFACTS.glob(f"{run_id}-events-*.jsonl"))
    for path in paths:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ShadowSafetyError(f"事件日志损坏：{path.name}:{line_no}") from exc
            if not isinstance(event, dict):
                raise ShadowSafetyError(f"事件日志格式错误：{path.name}:{line_no}")
            events.append(event)
    return events


def _chunks(values: list[int], size: int = 500):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _latest_successful_fields(
    events: list[dict[str, Any]],
) -> dict[tuple[int, str], tuple[tuple[int, float], Any]]:
    latest: dict[tuple[int, str], tuple[tuple[int, float], Any]] = {}
    for event in events:
        if int(event.get("status") or 0) != 200:
            continue
        source_id = int(event.get("source_id") or 0)
        order = (
            int(event.get("returned_revision") or 0),
            float(event.get("at") or 0),
        )
        for field, value in (event.get("changes") or {}).items():
            key = (source_id, str(field))
            if key not in latest or order >= latest[key][0]:
                latest[key] = (order, value)
    return latest


def _successful_revisions(events: list[dict[str, Any]]) -> dict[int, set[int]]:
    """Index committed revisions observed in the client event stream."""
    revisions: dict[int, set[int]] = defaultdict(set)
    for event in events:
        if int(event.get("status") or 0) != 200:
            continue
        source_id = int(event.get("source_id") or 0)
        revision = int(event.get("returned_revision") or 0)
        if source_id and revision:
            revisions[source_id].add(revision)
    return revisions


def _classify_field_verification(
    *,
    actual_revision: int,
    actual_value: Any,
    event_revision: int,
    expected_value: Any,
    successful_revisions: set[int],
    successful_operations: set[str],
    audit_after_values: list[tuple[str, dict[str, Any]]],
    field: str,
) -> str:
    if actual_value == expected_value:
        return "matched"
    if actual_revision <= event_revision:
        return "mismatch"
    observed_final_value = any(
        operation_id in successful_operations and after.get(field) == actual_value
        for operation_id, after in audit_after_values
    )
    if actual_revision in successful_revisions and observed_final_value:
        return "superseded"
    return "unrecorded"


def _conflict_groups(
    events: list[dict[str, Any]],
) -> dict[tuple[int, int, int], list[dict[str, Any]]]:
    groups: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("kind") != "conflict":
            continue
        groups[(
            int(event.get("pair") or 0),
            int(event.get("source_id") or 0),
            int(event.get("read_revision") or 0),
        )].append(event)
    return groups


def _verify_database(context: ShadowContext, events: list[dict[str, Any]]) -> dict[str, Any]:
    connection = _connect(context)
    issues: list[str] = []
    source_values: dict[int, dict[str, Any]] = {}
    audit_operations: dict[int, set[str]] = defaultdict(set)
    audit_after_values: dict[int, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    registration_links: dict[tuple[str, str], dict[str, Any]] = {}
    claimed_inspectors: dict[int, str] = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM _shadow_loadtest_marker "
                "WHERE run_id=%s AND environment='shadow'", (context.run_id,),
            )
            marker_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM _users WHERE username='observer@shadow' "
                "OR username LIKE 'loadtest-%%' OR username LIKE 'burst-%%'"
            )
            user_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM _shadow_loadtest_expectations WHERE run_id=%s",
                (context.run_id,),
            )
            expectation_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM _shadow_loadtest_expectations expectation "
                "JOIN _online_source_projection projection "
                "ON projection.parser_type COLLATE utf8mb4_unicode_ci="
                "expectation.parser_type COLLATE utf8mb4_unicode_ci "
                "AND projection.row_key COLLATE utf8mb4_unicode_ci="
                "expectation.row_key COLLATE utf8mb4_unicode_ci WHERE expectation.run_id=%s",
                (context.run_id,),
            )
            projection_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM registry_properties WHERE source_type='shadow_loadtest' "
                "AND source_ref LIKE %s", (f"shadow:{context.run_id}:property:%",),
            )
            property_count = int(cursor.fetchone()["count"])
            # ``create_local_source_row`` is intentionally idempotent by
            # business key.  If an earlier seed attempt created a row before
            # it was interrupted, a later seed reuses that canonical row and
            # keeps its original source_ref (for example t_fullchain:31).
            # Count the rows actually owned by this run through the immutable
            # expectation mapping instead of requiring every source_ref to be
            # run-scoped; otherwise a safe retry is reported as data loss.
            cursor.execute(
                "SELECT COUNT(*) AS count FROM _shadow_loadtest_expectations expectation "
                "JOIN _online_source_rows source ON source.id=expectation.source_id "
                "WHERE expectation.run_id=%s AND source.source_kind='local_table' "
                "AND source.spreadsheet_id=0 AND source.archived_at IS NULL",
                (context.run_id,),
            )
            source_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT status,COUNT(*) AS count,"
                "COALESCE(MAX(TIMESTAMPDIFF(SECOND,"
                "CASE WHEN status='running' THEN started_at ELSE created_at END,"
                "UTC_TIMESTAMP())),0) AS oldest_seconds "
                "FROM _online_projection_jobs GROUP BY status"
            )
            projection_queue = {
                str(row["status"]): {
                    "count": int(row["count"] or 0),
                    "oldest_seconds": int(row["oldest_seconds"] or 0),
                }
                for row in cursor.fetchall()
            }
            if int((projection_queue.get("failed") or {}).get("count") or 0):
                issues.append("projection_jobs_failed")
            running = projection_queue.get("running") or {}
            if int(running.get("count") or 0) and int(running.get("oldest_seconds") or 0) > 30:
                issues.append("projection_job_stalled")

            source_ids = sorted({int(event["source_id"]) for event in events if event.get("source_id")})
            for chunk in _chunks(source_ids):
                placeholders = ",".join(["%s"] * len(chunk))
                cursor.execute(
                    f"SELECT id,revision,values_json,parser_type,row_key,source_kind,source_ref "
                    f"FROM _online_source_rows WHERE id IN ({placeholders})",
                    chunk,
                )
                for row in cursor.fetchall():
                    raw = row["values_json"]
                    values = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    source_values[int(row["id"])] = {
                        "revision": int(row["revision"]), "values": values,
                        "parser_type": str(row["parser_type"]),
                        "row_key": str(row["row_key"]),
                        "source_kind": str(row["source_kind"] or ""),
                        "source_ref": str(row["source_ref"] or ""),
                    }

            source_id_chunks = list(_chunks(sorted(source_values)))
            for chunk in source_id_chunks:
                placeholders = ",".join(["%s"] * len(chunk))
                cursor.execute(
                    "SELECT expectation.source_id,local.revision,local.values_json "
                    "FROM _shadow_loadtest_expectations expectation "
                    "LEFT JOIN _online_source_rows source ON source.id=expectation.source_id "
                    "LEFT JOIN _local_source_records local "
                    "ON local.source_kind=source.source_kind AND local.source_ref=source.source_ref "
                    f"WHERE expectation.run_id=%s AND expectation.source_id IN ({placeholders})",
                    (context.run_id, *chunk),
                )
                for local in cursor.fetchall():
                    item = source_values.get(int(local["source_id"]))
                    if not item:
                        continue
                    item["local_revision"] = int(local["revision"]) if local["revision"] is not None else None
                    item["local_values"] = (
                        json.loads(local["values_json"])
                        if isinstance(local["values_json"], str)
                        else local["values_json"]
                    )
                cursor.execute(
                    "SELECT expectation.source_id,projection.source_revision,projection.values_json "
                    "FROM _shadow_loadtest_expectations expectation "
                    "LEFT JOIN _online_source_projection projection "
                    "ON projection.parser_type COLLATE utf8mb4_unicode_ci="
                    "expectation.parser_type COLLATE utf8mb4_unicode_ci "
                    "AND projection.row_key COLLATE utf8mb4_unicode_ci="
                    "expectation.row_key COLLATE utf8mb4_unicode_ci "
                    f"WHERE expectation.run_id=%s AND expectation.source_id IN ({placeholders})",
                    (context.run_id, *chunk),
                )
                for projection in cursor.fetchall():
                    item = source_values.get(int(projection["source_id"]))
                    if not item:
                        continue
                    item["projection_revision"] = (
                        int(projection["source_revision"])
                        if projection["source_revision"] is not None else None
                    )
                    item["projection_values"] = (
                        json.loads(projection["values_json"])
                        if isinstance(projection["values_json"], str)
                        else projection["values_json"]
                    )
                # Cross-check the durable audit trail as well as the Locust
                # event stream.  A response can be lost while the committed
                # transaction and its operation id remain present in the
                # database; that is different from an unrecorded write.
                cursor.execute(
                    "SELECT source.id AS source_id,a.operation_id,a.after_values "
                    "FROM _online_source_rows source "
                    "JOIN _online_writeback_audit a "
                    "ON a.parser_type=source.parser_type "
                    "AND a.row_key_after=source.row_key "
                    f"WHERE source.id IN ({placeholders})",
                    chunk,
                )
                for audit in cursor.fetchall():
                    sid = int(audit["source_id"])
                    operation_id = str(audit.get("operation_id") or "")
                    if operation_id:
                        audit_operations[sid].add(operation_id)
                    raw_after = audit.get("after_values")
                    if raw_after:
                        try:
                            parsed_after = json.loads(raw_after) if isinstance(raw_after, str) else raw_after
                        except (TypeError, ValueError):
                            parsed_after = None
                        if isinstance(parsed_after, dict):
                            audit_after_values[sid].append((operation_id, parsed_after))

            failed_operation_ids = sorted({
                str(event.get("failed_operation_id") or "")
                for event in events
                if int(event.get("status") or 0) >= 500
                and str(event.get("failed_operation_id") or "")
            })
            partial_failed_operations: list[str] = []
            for operation_id in failed_operation_ids:
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM _online_writeback_audit WHERE operation_id=%s",
                    (operation_id,),
                )
                audit_count = int(cursor.fetchone()["count"])
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM _online_projection_jobs WHERE operation_id=%s",
                    (operation_id,),
                )
                queue_count = int(cursor.fetchone()["count"])
                if audit_count or queue_count:
                    partial_failed_operations.append(operation_id)

            registration_keys = sorted({
                (str(event.get("parser_type")), str(event.get("row_key")))
                for event in events
                if event.get("scenario") == "pending_registration" and event.get("status") == 200
            })
            for parser_type, row_key in registration_keys:
                cursor.execute(
                    "SELECT property_id,property_version,status FROM _task_registration_links "
                    "WHERE parser_type=%s AND row_key=%s", (parser_type, row_key),
                )
                row = cursor.fetchone()
                if row:
                    registration_links[(parser_type, row_key)] = row

            claimed_source_ids = sorted({
                int(event.get("source_id") or 0)
                for event in events
                if event.get("kind") == "claim" and event.get("source_id")
            })
            for chunk in _chunks(claimed_source_ids):
                placeholders = ",".join(["%s"] * len(chunk))
                cursor.execute(
                    "SELECT expectation.source_id,projection.inspector "
                    "FROM _shadow_loadtest_expectations expectation "
                    "JOIN _online_source_projection projection "
                    "ON projection.parser_type COLLATE utf8mb4_unicode_ci="
                    "expectation.parser_type COLLATE utf8mb4_unicode_ci "
                    "AND projection.row_key COLLATE utf8mb4_unicode_ci="
                    "expectation.row_key COLLATE utf8mb4_unicode_ci "
                    f"WHERE expectation.run_id=%s AND expectation.source_id IN ({placeholders})",
                    (context.run_id, *chunk),
                )
                for row in cursor.fetchall():
                    claimed_inspectors[int(row["source_id"])] = str(row["inspector"] or "")
    finally:
        connection.close()

    expected_counts = {
        "marker": 1, "users": 76, "expectations": 3600,
        "projections": 3600, "properties": 48, "sources": 3600,
    }
    actual_counts = {
        "marker": marker_count, "users": user_count,
        "expectations": expectation_count, "projections": projection_count,
        "properties": property_count, "sources": source_count,
    }
    for key, expected in expected_counts.items():
        if actual_counts[key] != expected:
            issues.append(f"{key}:{actual_counts[key]}!={expected}")
    for operation_id in partial_failed_operations:
        issues.append(f"failed_transaction_partial_write:{operation_id}")

    successful = [event for event in events if int(event.get("status") or 0) == 200]
    for event in successful:
        read_revision = int(event.get("read_revision") or 0)
        returned_revision = int(event.get("returned_revision") or 0)
        if returned_revision <= read_revision:
            issues.append(f"revision_not_advanced:{event.get('source_id')}")

    latest_field = _latest_successful_fields(events)
    successful_revisions = _successful_revisions(events)
    successful_operations: dict[int, set[str]] = defaultdict(set)
    for event in successful:
        sid = int(event.get("source_id") or 0)
        operation_id = str(event.get("operation_id") or "")
        if sid and operation_id:
            successful_operations[sid].add(operation_id)
    unrecorded_writes: set[int] = set()
    superseded_writes: set[int] = set()
    for (source_id, field), (event_order, expected_value) in latest_field.items():
        source_item = source_values.get(source_id) or {}
        actual = source_item.get("values", {}).get(field)
        outcome = _classify_field_verification(
            actual_revision=int(source_item.get("revision") or 0),
            actual_value=actual,
            event_revision=int(event_order[0]),
            expected_value=expected_value,
            successful_revisions=successful_revisions.get(source_id, set()),
            successful_operations=successful_operations.get(source_id, set()),
            audit_after_values=audit_after_values.get(source_id, []),
            field=field,
        )
        if outcome == "superseded":
            superseded_writes.add(source_id)
        elif outcome == "unrecorded":
            issues.append(f"unrecorded_write:{source_id}:{field}")
            unrecorded_writes.add(source_id)
        elif outcome == "mismatch":
            issues.append(f"saved_value_mismatch:{source_id}:{field}")

    for source_id, item in source_values.items():
        revision = int(item.get("revision") or 0)
        if item.get("local_revision") != revision:
            issues.append(f"local_source_revision_mismatch:{source_id}")
        if item.get("local_values") != item.get("values"):
            issues.append(f"local_source_values_mismatch:{source_id}")
        if item.get("projection_revision") != revision:
            issues.append(f"projection_revision_mismatch:{source_id}")
        if item.get("projection_values") != item.get("values"):
            issues.append(f"projection_values_mismatch:{source_id}")

    claim_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("kind") == "claim":
            claim_groups[int(event.get("source_id") or 0)].append(event)
    verified_claims = 0
    for source_id, group in claim_groups.items():
        winners = [event for event in group if int(event.get("status") or 0) == 200]
        if len(winners) != 1:
            statuses = [int(event.get("status") or 0) for event in group]
            # A task may already have been claimed by an earlier load stage.
            # A later attempt then correctly returns only 409 responses; this
            # is expected contention, not evidence of a missing winner.
            if not winners and statuses and all(status == 409 for status in statuses):
                continue
            issues.append(f"claim_winner_count_invalid:{source_id}:{len(winners)}")
            continue
        expected_inspector = str(winners[0].get("inspector") or "")
        if claimed_inspectors.get(source_id) != expected_inspector:
            issues.append(f"claim_inspector_mismatch:{source_id}")
            continue
        verified_claims += 1

    latest_registration: dict[tuple[str, str], dict[str, Any]] = {}
    for event in successful:
        if event.get("scenario") != "pending_registration":
            continue
        key = (str(event.get("parser_type")), str(event.get("row_key")))
        current = latest_registration.get(key)
        if current is None or (
            int(event.get("returned_revision") or 0), float(event.get("at") or 0)
        ) >= (
            int(current.get("returned_revision") or 0), float(current.get("at") or 0)
        ):
            latest_registration[key] = event
    for key, event in latest_registration.items():
        link = registration_links.get(key)
        if not link:
            issues.append(f"registration_link_missing:{key[0]}:{key[1]}")
            continue
        if int(link["property_id"] or 0) != int(event.get("property_id") or 0):
            issues.append(f"registration_property_mismatch:{key[0]}:{key[1]}")
        if int(link["property_version"] or 0) != int(event.get("property_version") or 0):
            issues.append(f"registration_property_version_mismatch:{key[0]}:{key[1]}")
        link_status = str(link["status"] or "")
        if link_status not in {"awaiting_match", "matched_once"}:
            # A subsequent ordinary edit may intentionally move a task away
            # from 待登记, which cancels its previous property link.  Accept
            # that terminal state only when the current business value is no
            # longer 待登记; otherwise retain the consistency error.
            source_item = next(
                (item for item in source_values.values()
                 if item.get("parser_type") == key[0] and item.get("row_key") == key[1]),
                None,
            )
            current_values = (source_item or {}).get("values") or {}
            result_value = str(current_values.get("核查结果") or current_values.get("result") or "")
            if link_status != "cancelled" or result_value == "待登记":
                issues.append(f"registration_status_invalid:{key[0]}:{key[1]}")

    conflict_groups = _conflict_groups(events)
    verified_rounds = 0
    latest_conflict_winner: dict[int, dict[str, Any]] = {}
    for key, group in conflict_groups.items():
        statuses = sorted(int(event.get("status") or 0) for event in group)
        if statuses != [200, 409]:
            issues.append(f"conflict_round_invalid:{key}:{statuses}")
        else:
            verified_rounds += 1
            winner = next(event for event in group if int(event.get("status") or 0) == 200)
            source_id = int(winner.get("source_id") or 0)
            current = latest_conflict_winner.get(source_id)
            if current is None or (
                int(winner.get("returned_revision") or 0), float(winner.get("at") or 0)
            ) >= (
                int(current.get("returned_revision") or 0), float(current.get("at") or 0)
            ):
                latest_conflict_winner[source_id] = winner
    for source_id, winner in latest_conflict_winner.items():
        actual_values = (source_values.get(source_id) or {}).get("values", {})
        for field, expected_value in (winner.get("changes") or {}).items():
            if actual_values.get(field) != expected_value:
                issues.append(f"conflict_winner_value_mismatch:{source_id}:{field}")

    return {
        "counts": actual_counts,
        "events": len(events),
        "successful_writes": len(successful),
        "expected_conflicts": sum(1 for event in events if int(event.get("status") or 0) == 409),
        "verified_conflict_rounds": verified_rounds,
        "verified_claims": verified_claims,
        "unrecorded_write_sources": len(unrecorded_writes),
        "superseded_write_sources": len(superseded_writes),
        "verified_failed_transactions": len(failed_operation_ids),
        "projection_queue": projection_queue,
        "issues": issues,
    }


def _verify_production_proof(path: Path | None, run_id: str) -> dict[str, Any]:
    if path is None:
        return {"provided": False, "note": "正式库零压测数据证据需由独立只读扫描生成"}
    data = json.loads(path.read_text(encoding="utf-8"))
    if str(data.get("run_id") or "").upper() != run_id:
        raise ShadowSafetyError("正式库只读证明的 run_id 不一致")
    if int(data.get("matching_rows", -1)) != 0:
        raise ShadowSafetyError("正式库只读证明发现压测数据，必须立即停止")
    checked_scopes = {str(item) for item in data.get("checked_scopes") or []}
    if not data.get("checked_at") or not REQUIRED_PRODUCTION_PROOF_SCOPES <= checked_scopes:
        raise ShadowSafetyError("正式库只读证明缺少检查时间或范围")
    scope_counts = data.get("scope_counts")
    if not isinstance(scope_counts, dict):
        raise ShadowSafetyError("正式库只读证明缺少逐范围计数")
    for scope in REQUIRED_PRODUCTION_PROOF_SCOPES:
        try:
            count = int(scope_counts[scope])
        except (KeyError, TypeError, ValueError) as exc:
            raise ShadowSafetyError(f"正式库只读证明缺少有效计数：{scope}") from exc
        if count != 0:
            raise ShadowSafetyError(f"正式库只读证明发现压测数据：{scope}")
    return {"provided": True, "matching_rows": 0,
            "checked_at": data["checked_at"], "checked_scopes": sorted(checked_scopes),
            "scope_counts": {scope: 0 for scope in sorted(REQUIRED_PRODUCTION_PROOF_SCOPES)}}


def verify(run_id: str, production_proof: Path | None, event_log: Path | None = None) -> int:
    context = require_shadow_context(run_id)
    _verify_shadow_schema(context, seeded=True)
    _verify_projection_performance_schema(context)
    _verify_marker(context, allow_placeholder=False)
    path = _manifest(context.run_id)
    runtime_path = _runtime_index(context.run_id)
    if not path.is_file() or not runtime_path.is_file():
        raise ShadowSafetyError("找不到影子清单或真实运行索引")
    data = json.loads(path.read_text(encoding="utf-8"))
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    tasks = data.get("tasks") or []
    expected_manifest = {"users": 76, "tasks": 3600, "communities": 12, "properties": 48}
    actual_manifest = {
        "users": len(data.get("users") or []),
        "tasks": len(tasks),
        "communities": len(data.get("communities") or []),
        "properties": sum(len(item.get("properties") or []) for item in data.get("communities") or []),
    }
    if actual_manifest != expected_manifest or len(runtime.get("tasks") or []) != 3600:
        raise ShadowSafetyError(f"影子清单数量不符合预期：{actual_manifest}")
    state_counts = defaultdict(int)
    for task in make_tasks():
        state_counts[str(task["state"])] += 1
    selected_event_log = event_log
    if selected_event_log is None and _last_stage_index(context.run_id).is_file():
        stage = json.loads(_last_stage_index(context.run_id).read_text(encoding="utf-8"))
        if str(stage.get("run_id") or "").upper() != context.run_id:
            raise ShadowSafetyError("最近阶段索引的运行编号不一致")
        selected_event_log = Path(str(stage.get("event_log") or ""))
    events = _load_events(context.run_id, selected_event_log)
    database = _verify_database(context, events)
    production = _verify_production_proof(production_proof, context.run_id)
    result = {
        "run_id": context.run_id,
        "manifest_counts": actual_manifest,
        "state_counts": dict(state_counts),
        "database": database,
        "production_zero_data_proof": production,
        "event_scope": str(selected_event_log) if selected_event_log else "all_run_events",
        "passed": not database["issues"],
    }
    output = ARTIFACTS / f"{context.run_id}-verify.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 4


def cleanup(run_id: str) -> int:
    context = require_shadow_context(run_id)
    _verify_marker(context, allow_placeholder=True)
    result = subprocess.run(
        _compose_command(context, "down", "--volumes", "--remove-orphans"),
        cwd=ROOT, check=False,
    )
    if result.returncode:
        return result.returncode
    exact_files = {_manifest(context.run_id), _runtime_index(context.run_id)}
    for path in exact_files:
        if path.is_file() and path.parent.resolve() == ARTIFACTS.resolve():
            path.unlink()
    for path in ARTIFACTS.glob(f"{context.run_id}-*"):
        if path.parent.resolve() != ARTIFACTS.resolve():
            raise ShadowSafetyError("清理目标逸出 artifacts 目录")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    print(json.dumps({
        "run_id": context.run_id,
        "cleanup": "shadow_project_and_run_artifacts_removed",
        "docker_project": context.project,
    }, ensure_ascii=False))
    return 0


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "seed":
            return seed(args.run_id)
        if args.command == "run":
            return run(args.run_id, args.users, args.duration, args.scenario)
        if args.command == "verify":
            return verify(args.run_id, args.production_proof, args.event_log)
        return cleanup(args.run_id)
    except (ShadowSafetyError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"安全检查失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
