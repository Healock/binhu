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
from typing import Any
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
REQUIRED_SHADOW_TABLES = {
    "_daily_report_meta",
    "_daily_task_ledger",
    "_sessions",
    "_user_presence_clients",
    "_users",
    "_online_source_projection",
    "_shadow_loadtest_marker",
    "_shadow_loadtest_expectations",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="滨湖智慧平台影子压测安全工具")
    parser.add_argument("command", choices=("seed", "run", "verify", "cleanup"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", choices=("login", "mixed", "conflict"), default="mixed")
    parser.add_argument("--users", type=int, default=50, choices=(5, 20, 50, 75))
    parser.add_argument("--duration", default="30m")
    parser.add_argument("--production-proof", type=Path)
    return parser


def _manifest(run_id: str) -> Path:
    return ARTIFACTS / f"shadow-fixture-{run_id}.json"


def _runtime_index(run_id: str) -> Path:
    return ARTIFACTS / f"shadow-runtime-{run_id}.json"


def _require_database_credentials() -> tuple[str, str]:
    user = os.environ.get("SHADOW_DB_USER", "").strip()
    password = os.environ.get("SHADOW_DB_PASSWORD", "")
    if not user or not password:
        raise ShadowSafetyError("缺少 SHADOW_DB_USER 或 SHADOW_DB_PASSWORD")
    return user, password


def _connect(context: ShadowContext):
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
        database=context.db_name,
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


def _verify_shadow_schema(context: ShadowContext) -> None:
    """Fail closed before seed or traffic if the isolated schema is partial."""
    connection = _connect(context)
    try:
        with connection.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(REQUIRED_SHADOW_TABLES))
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN (" + placeholders + ")",
                (context.db_name, *sorted(REQUIRED_SHADOW_TABLES)),
            )
            present = {str(row["TABLE_NAME"]) for row in cursor.fetchall()}
    finally:
        connection.close()
    missing = sorted(REQUIRED_SHADOW_TABLES - present)
    if missing:
        raise ShadowSafetyError(
            "影子环境 schema 不完整，缺少必需表：" + ", ".join(missing)
        )


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
    _verify_shadow_schema(context)
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
        production_health_url=production_health,
        production_containers=production_containers,
    ))
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


def _load_events(run_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted(ARTIFACTS.glob(f"{run_id}-events-*.jsonl")):
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

            source_ids = sorted({int(event["source_id"]) for event in events if event.get("source_id")})
            for chunk in _chunks(source_ids):
                placeholders = ",".join(["%s"] * len(chunk))
                cursor.execute(
                    f"SELECT id,revision,values_json FROM _online_source_rows WHERE id IN ({placeholders})",
                    chunk,
                )
                for row in cursor.fetchall():
                    raw = row["values_json"]
                    values = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    source_values[int(row["id"])] = {
                        "revision": int(row["revision"]), "values": values,
                    }

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

    successful = [event for event in events if int(event.get("status") or 0) == 200]
    for event in successful:
        read_revision = int(event.get("read_revision") or 0)
        returned_revision = int(event.get("returned_revision") or 0)
        if returned_revision <= read_revision:
            issues.append(f"revision_not_advanced:{event.get('source_id')}")

    latest_field = _latest_successful_fields(events)
    for (source_id, field), (_, expected_value) in latest_field.items():
        actual = (source_values.get(source_id) or {}).get("values", {}).get(field)
        if actual != expected_value:
            issues.append(f"saved_value_mismatch:{source_id}:{field}")

    claim_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("kind") == "claim":
            claim_groups[int(event.get("source_id") or 0)].append(event)
    verified_claims = 0
    for source_id, group in claim_groups.items():
        winners = [event for event in group if int(event.get("status") or 0) == 200]
        if len(winners) != 1:
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
        if str(link["status"] or "") not in {"awaiting_match", "matched_once"}:
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


def verify(run_id: str, production_proof: Path | None) -> int:
    context = require_shadow_context(run_id)
    _verify_shadow_schema(context)
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
    events = _load_events(context.run_id)
    database = _verify_database(context, events)
    production = _verify_production_proof(production_proof, context.run_id)
    result = {
        "run_id": context.run_id,
        "manifest_counts": actual_manifest,
        "state_counts": dict(state_counts),
        "database": database,
        "production_zero_data_proof": production,
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
            return verify(args.run_id, args.production_proof)
        return cleanup(args.run_id)
    except (ShadowSafetyError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"安全检查失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
