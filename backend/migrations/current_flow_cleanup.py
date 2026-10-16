"""受控归档所有在线业务表中的当前任务。

维护日期只用于运行证据和工作流审计，不过滤业务行。``measure`` 会按
``TASK_WORKFLOWS`` 逐类型建立脱敏清单；``apply`` 在同一事务中把清单中的
当前行移入对应 ``OnlineDataArchive`` 表，并清理本地来源、投影、队列和任务图。
日报、审计、备份及外部历史材料不会被删除。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    from services.parsers import get_parser
    from services.task_workflow import TASK_WORKFLOWS
except ModuleNotFoundError:  # package import used by deployment contract tests
    from backend.services.parsers import get_parser
    from backend.services.task_workflow import TASK_WORKFLOWS

ARCHIVE_REASON = "current_flow_cleanup_all"
SAFE_RUN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")
DATABASES = (
    "OnlineData", "OnlineDataArchive", "daily_report", "PlatformData",
    "VisitData", "DispatchData", "RegistryData", "WorkflowData",
)


class CleanupError(RuntimeError):
    pass


def should_archive_current_flow(values: dict[str, Any]) -> bool:
    """Compatibility predicate: every measured live row is in scope."""
    del values
    return True


def parse_business_date(value: Any) -> date | None:
    text = str(value or "").strip().replace("年", "-").replace("月", "-").replace("日", "")
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def digest_row(values: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def evidence_root(run_id: str) -> Path:
    root = Path(os.environ.get("BINHU_CLEANUP_EVIDENCE_ROOT", "/srv"))
    return (root / f"binhu-release-evidence-current-flow-cleanup-{run_id}").resolve()


def _json_dump(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def flow_specs() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for parser_type, workflow in TASK_WORKFLOWS.items():
        parser = get_parser(parser_type)
        specs.append({
            "parser_type": parser_type,
            "table": parser.table_name,
            "archive_table": f"{parser.table_name}_archive",
            "parser": parser,
            "workflow": workflow,
        })
    return specs


async def connect():
    import aiomysql
    return await aiomysql.connect(
        host=os.environ.get("MYSQL_HOST", "mysql"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "binhu"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        db="OnlineData", autocommit=False, charset="utf8mb4",
    )


async def query_one(cur, sql: str, params: tuple[Any, ...] = ()):
    await cur.execute(sql, params)
    return await cur.fetchone()


async def query_all(cur, sql: str, params: tuple[Any, ...] = ()):
    await cur.execute(sql, params)
    return await cur.fetchall()


async def table_exists(cur, schema: str, table: str) -> bool:
    return bool(await query_one(cur, "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s", (schema, table)))


async def columns(cur, schema: str, table: str) -> set[str]:
    return {str(row[0]) for row in await query_all(cur, "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s", (schema, table))}


def placeholders(values: list[Any]) -> str:
    return ",".join(["%s"] * len(values)) or "NULL"


async def load_targets(cur) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for spec in flow_specs():
        table = spec["table"]
        if not await table_exists(cur, "OnlineData", table):
            raise CleanupError(f"table_missing:{table}")
        available = await columns(cur, "OnlineData", table)
        if "id" not in available or "_row_key" not in available:
            raise CleanupError(f"table_schema_invalid:{table}")
        selected = ["id", "_row_key"] + [name for name in spec["parser"].COLUMNS if name in available]
        selected = list(dict.fromkeys(selected))
        quoted = ",".join(f"`{name}`" for name in selected)
        for raw in await query_all(cur, f"SELECT {quoted} FROM `OnlineData`.`{table}`"):
            values = dict(zip(selected, raw))
            row_key = str(values.get("_row_key") or "").strip()
            if not row_key:
                continue
            targets.append({
                "parser_type": spec["parser_type"], "table": table,
                "archive_table": spec["archive_table"], "id": int(values["id"]),
                "row_key": row_key, "row_digest": digest_row(values),
            })
    return targets


async def related_counts(cur, targets: list[dict[str, Any]]) -> dict[str, int]:
    result = {"business": len(targets), "source": 0, "projection": 0, "address_match": 0, "local_source": 0, "review_flow": 0, "registration": 0, "graph_nodes": 0}
    for parser_type, rows in _group_targets(targets).items():
        keys = [item["row_key"] for item in rows]
        ph = placeholders(keys)
        result["source"] += int((await query_one(cur, f"SELECT COUNT(*) FROM `_online_source_rows` WHERE parser_type=%s AND row_key IN ({ph}) AND archived_at IS NULL", (parser_type, *keys)))[0])
        result["projection"] += int((await query_one(cur, f"SELECT COUNT(*) FROM `_online_source_projection` WHERE parser_type=%s AND row_key IN ({ph})", (parser_type, *keys)))[0])
        result["address_match"] += int((await query_one(cur, f"SELECT COUNT(*) FROM `_online_task_address_matches` WHERE parser_type=%s AND row_key IN ({ph})", (parser_type, *keys)))[0])
        result["local_source"] += int((await query_one(cur, f"SELECT COUNT(*) FROM `_local_source_records` WHERE parser_type=%s AND business_key IN ({ph}) AND archived_at IS NULL", (parser_type, *keys)))[0])
        result["review_flow"] += int((await query_one(cur, f"SELECT COUNT(*) FROM `_unverifiable_review_flows` WHERE parser_type=%s AND row_key IN ({ph}) AND archived_at IS NULL", (parser_type, *keys)))[0])
        result["registration"] += int((await query_one(cur, f"SELECT COUNT(*) FROM `_task_registration_links` WHERE parser_type=%s AND row_key IN ({ph})", (parser_type, *keys)))[0])
        workflow_db = os.environ.get("MYSQL_WORKFLOW_DB", "WorkflowData")
        if await table_exists(cur, workflow_db, "task_graph_nodes"):
            result["graph_nodes"] += int((await query_one(cur, f"SELECT COUNT(*) FROM `{workflow_db}`.`task_graph_nodes` WHERE provider='online' AND parser_type=%s AND source_ref IN ({ph}) AND archived_at IS NULL", (parser_type, *keys)))[0])
    return result


def _group_targets(targets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for target in targets:
        grouped.setdefault(str(target["parser_type"]), []).append(target)
    return grouped


async def load_manifest(root: Path) -> dict[str, Any]:
    path = root / "target.json"
    if not path.is_file():
        raise CleanupError("target_manifest_missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CleanupError("target_manifest_invalid") from exc
    if payload.get("scope") != "all_current_workflows" or not SAFE_RUN.fullmatch(str(payload.get("run_id") or "")):
        raise CleanupError("target_manifest_scope_invalid")
    return payload


def check_backup_root(root: Path) -> dict[str, Any]:
    candidates = sorted(root.glob("**/backup-manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for manifest_path in candidates:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = {str(item.get("database")): item for item in manifest.get("databases", [])}
            if not set(DATABASES).issubset(entries):
                continue
            files: list[dict[str, Any]] = []
            for database in DATABASES:
                entry = entries[database]
                path = manifest_path.parent / str(entry.get("filename") or "")
                sha_path = Path(str(path) + ".sha256")
                if not path.is_file() or not sha_path.is_file() or path.stat().st_size <= 0:
                    raise ValueError
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                recorded = sha_path.read_text(encoding="ascii").split()[0]
                if digest != str(entry.get("sha256") or "") or digest != recorded:
                    raise ValueError
                files.append({"database": database, "size_bytes": path.stat().st_size, "sha256": digest})
            age = max(0, datetime.now(timezone.utc).timestamp() - manifest_path.stat().st_mtime)
            if age <= int(os.environ.get("BINHU_CLEANUP_MAX_BACKUP_AGE_SECONDS", "86400")):
                return {"backup_id": manifest_path.parent.name, "manifest": str(manifest_path), "age_seconds": int(age), "files": files}
        except (OSError, ValueError, TypeError, IndexError, json.JSONDecodeError):
            continue
    raise CleanupError("backup_missing_or_invalid")


async def archive_flow(cur, spec: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    keys = [str(item["row_key"]) for item in rows]
    ph = placeholders(keys)
    table, archive = spec["table"], spec["archive_table"]
    current_cols = await columns(cur, "OnlineData", table)
    archive_cols = await columns(cur, "OnlineDataArchive", archive)
    if not archive_cols:
        raise CleanupError(f"archive_table_missing:{archive}")
    # Resolve canonical parser fields to physical database aliases first.  For
    # example, 疑似返苏 historically stores 身份证号码/联系号码 while newer
    # schemas use the canonical labels.  The archive tables are LIKE the live
    # tables, so the resolved physical names are valid on both sides.
    try:
        resolved = spec["parser"].resolve_database_columns(current_cols)
    except RuntimeError as exc:
        raise CleanupError(f"table_schema_invalid:{table}") from exc
    copy_cols = ["_row_key"] + [resolved[name] for name in spec["parser"].COLUMNS if resolved.get(name) in archive_cols]
    copy_cols = list(dict.fromkeys(name for name in copy_cols if name not in {"id", "_archived_at", "_archive_reason"}))
    if "_row_key" not in copy_cols or "_archive_reason" not in archive_cols:
        raise CleanupError(f"archive_schema_invalid:{archive}")
    quoted = ",".join(f"`{name}`" for name in copy_cols)
    select = ",".join(f"current.`{name}`" for name in copy_cols)
    await cur.execute(
        f"INSERT INTO `OnlineDataArchive`.`{archive}` ({quoted},`_archive_reason`) "
        f"SELECT {select},%s FROM `OnlineData`.`{table}` current "
        f"WHERE current.`_row_key` IN ({ph}) AND NOT EXISTS "
        f"(SELECT 1 FROM `OnlineDataArchive`.`{archive}` archived WHERE archived.`_row_key`=current.`_row_key` AND archived.`_archive_reason`=%s)",
        (ARCHIVE_REASON, *keys, ARCHIVE_REASON),
    )
    await cur.execute(f"DELETE FROM `OnlineData`.`{table}` WHERE `_row_key` IN ({ph})", tuple(keys))
    await cur.execute(f"UPDATE `_online_source_rows` SET archived_at=UTC_TIMESTAMP(), revision=revision+1, source_kind='current_flow_cleanup' WHERE parser_type=%s AND row_key IN ({ph}) AND archived_at IS NULL", (spec["parser_type"], *keys))
    await cur.execute(f"UPDATE `_local_source_records` SET status='archived', archived_at=UTC_TIMESTAMP(), updated_at=UTC_TIMESTAMP() WHERE parser_type=%s AND business_key IN ({ph}) AND archived_at IS NULL", (spec["parser_type"], *keys))
    for auxiliary in ("_online_source_projection", "_online_task_address_matches"):
        await cur.execute(f"DELETE FROM `{auxiliary}` WHERE parser_type=%s AND row_key IN ({ph})", (spec["parser_type"], *keys))
    for auxiliary in ("_online_local_changes", "_online_projection_jobs", "_online_summary_updates"):
        await cur.execute(f"UPDATE `{auxiliary}` SET status='cancelled', error_code='current_flow_archived', updated_at=UTC_TIMESTAMP() WHERE parser_type=%s AND row_key IN ({ph}) AND status IN ('pending','processing','queued','running','retry')", (spec["parser_type"], *keys))
    await cur.execute(f"UPDATE `_task_registration_links` SET status='cancelled', reason_code='source_missing', updated_at=UTC_TIMESTAMP() WHERE parser_type=%s AND row_key IN ({ph})", (spec["parser_type"], *keys))
    await cur.execute(f"UPDATE `_unverifiable_review_flows` SET archived_at=UTC_TIMESTAMP(), updated_at=UTC_TIMESTAMP() WHERE parser_type=%s AND row_key IN ({ph}) AND archived_at IS NULL", (spec["parser_type"], *keys))
    workflow_db = os.environ.get("MYSQL_WORKFLOW_DB", "WorkflowData")
    if await table_exists(cur, workflow_db, "task_graph_nodes"):
        await cur.execute(f"UPDATE `{workflow_db}`.`task_graph_nodes` SET status='source_missing', reason_code='current_flow_archived', archived_at=UTC_TIMESTAMP(), updated_at=UTC_TIMESTAMP() WHERE provider='online' AND parser_type=%s AND source_ref IN ({ph}) AND archived_at IS NULL", (spec["parser_type"], *keys))
        await cur.execute(f"UPDATE `{workflow_db}`.`task_graph_dependencies` dependency JOIN `{workflow_db}`.`task_graph_nodes` predecessor ON predecessor.id=dependency.predecessor_node_id JOIN `{workflow_db}`.`task_graph_nodes` successor ON successor.id=dependency.successor_node_id SET dependency.state='cancelled', dependency.reason_code='current_flow_archived', dependency.cancelled_at=COALESCE(dependency.cancelled_at,UTC_TIMESTAMP()), dependency.updated_at=UTC_TIMESTAMP() WHERE dependency.state='active' AND ((predecessor.parser_type=%s AND predecessor.source_ref IN ({ph})) OR (successor.parser_type=%s AND successor.source_ref IN ({ph})))", (spec["parser_type"], *keys, spec["parser_type"], *keys))


async def run(phase: str, run_id: str, business_date: date) -> dict[str, Any]:
    root = evidence_root(run_id)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if phase == "backup-check":
        backup = check_backup_root(Path(os.environ.get("BINHU_DEPLOY_BACKUP_ROOT", "/root/binhu/deploy-backups/automated")))
        _json_dump(root / "backup-check.json", backup)
        return {"phase": phase, "backup_id": backup["backup_id"]}
    conn = await connect()
    try:
        async with conn.cursor() as cur:
            if phase == "measure":
                targets = await load_targets(cur)
                grouped = _group_targets(targets)
                source_ids: list[dict[str, Any]] = []
                for parser_type, rows in grouped.items():
                    keys = [item["row_key"] for item in rows]
                    for row in await query_all(cur, f"SELECT id,row_key FROM `_online_source_rows` WHERE parser_type=%s AND row_key IN ({placeholders(keys)}) AND archived_at IS NULL", (parser_type, *keys)):
                        source_ids.append({"parser_type": parser_type, "id": int(row[0]), "row_key": str(row[1])})
                payload = {"schema": 2, "scope": "all_current_workflows", "run_id": run_id, "business_date": business_date.isoformat(), "targets": targets, "source_ids": source_ids, "counts": await related_counts(cur, targets), "workflow_types": sorted(grouped), "measured_at": datetime.now(timezone.utc).isoformat()}
                _json_dump(root / "target.json", payload)
                _json_dump(root / "measure.json", {"target_count": len(targets), "workflow_counts": {key: len(value) for key, value in grouped.items()}, "counts": payload["counts"]})
                return {"phase": phase, "target_count": len(targets), "workflow_counts": {key: len(value) for key, value in grouped.items()}, "counts": payload["counts"]}
            manifest = await load_manifest(root)
            if phase == "prepare":
                backup = check_backup_root(Path(os.environ.get("BINHU_DEPLOY_BACKUP_ROOT", "/root/binhu/deploy-backups/automated")))
                manifest["backup"] = backup
                _json_dump(root / "target.json", manifest)
                _json_dump(root / "prepare.json", {"ready": True, "backup_id": backup["backup_id"], "target_count": len(manifest.get("targets", []))})
                return {"phase": phase, "ready": True, "target_count": len(manifest.get("targets", []))}
            if phase == "verify":
                grouped = _group_targets(manifest.get("targets", []))
                remaining = 0
                archived = 0
                for spec in flow_specs():
                    rows = grouped.get(spec["parser_type"], [])
                    if not rows:
                        continue
                    keys = [item["row_key"] for item in rows]
                    ph = placeholders(keys)
                    remaining += int((await query_one(cur, f"SELECT COUNT(*) FROM `OnlineData`.`{spec['table']}` WHERE `_row_key` IN ({ph})", tuple(keys)))[0])
                    archived += int((await query_one(cur, f"SELECT COUNT(*) FROM `OnlineDataArchive`.`{spec['archive_table']}` WHERE `_archive_reason`=%s AND `_row_key` IN ({ph})", (ARCHIVE_REASON, *keys)))[0])
                after = await related_counts(cur, manifest.get("targets", []))
                payload = {"ready": remaining == 0 and archived == len(manifest.get("targets", [])) and all(after[key] == 0 for key in ("source", "projection", "address_match", "local_source", "review_flow", "registration", "graph_nodes")), "remaining_target_business": remaining, "archived_count": archived, "after_counts": after}
                _json_dump(root / "verify.json", payload)
                if not payload["ready"]:
                    raise CleanupError("verification_failed")
                return {"phase": phase, **payload}
            if phase != "apply":
                raise CleanupError("unsupported_phase")
            if not (root / "prepare.json").is_file():
                raise CleanupError("prepare_required")
            grouped = _group_targets(manifest.get("targets", []))
            await cur.execute("SELECT GET_LOCK('binhu-current-flow-cleanup', 10)")
            lock = await cur.fetchone()
            if not lock or int(lock[0]) != 1:
                raise CleanupError("maintenance_lock_unavailable")
            try:
                specs = {spec["parser_type"]: spec for spec in flow_specs()}
                for parser_type, rows in grouped.items():
                    await archive_flow(cur, specs[parser_type], rows)
                await conn.commit()
                _json_dump(root / "apply.json", {"status": "success", "target_count": len(manifest.get("targets", [])), "workflow_counts": {key: len(value) for key, value in grouped.items()}, "archive_reason": ARCHIVE_REASON})
                return {"phase": phase, "status": "success", "target_count": len(manifest.get("targets", []))}
            finally:
                await cur.execute("SELECT RELEASE_LOCK('binhu-current-flow-cleanup')")
    except Exception:
        await conn.rollback()
        _json_dump(root / "failure.json", {"error_code": "cleanup_failed", "phase": phase})
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("measure", "backup-check", "prepare", "apply", "verify"))
    parser.add_argument("run_id")
    parser.add_argument("business_date")
    args = parser.parse_args(argv)
    if not SAFE_RUN.fullmatch(args.run_id):
        print("invalid run id", file=sys.stderr)
        return 64
    try:
        target_date = date.fromisoformat(args.business_date)
    except ValueError:
        print("invalid business date", file=sys.stderr)
        return 64
    try:
        result = asyncio.run(run(args.phase, args.run_id, target_date))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except CleanupError as exc:
        print(json.dumps({"error_code": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"error_code": "cleanup_failed"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
