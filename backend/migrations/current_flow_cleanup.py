"""受控的「流口指令核查」当前任务按业务日期归档工具。

该命令只允许在生产固定部署网关中调用。默认只处理全链条（流口指令核查）
且只处理 ``下发日期`` 等于指定日期的当前任务。流程分为 measure、backup-check、
prepare、apply、verify 五个不可跳过的阶段；apply 使用同一 MySQL 事务，失败时
整体回滚。输出和证据只包含数量、哈希和安全错误码，不写出业务正文。
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

import aiomysql


PARSER_TYPE = "全链条"
TABLE = "t_fullchain"
ARCHIVE_TABLE = "t_fullchain_archive"
DATE_FIELDS = ("下发日期", "下发时间", "日期")
DATABASES = (
    "OnlineData",
    "OnlineDataArchive",
    "daily_report",
    "PlatformData",
    "VisitData",
    "DispatchData",
    "RegistryData",
    "WorkflowData",
)
SAFE_RUN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")


class CleanupError(RuntimeError):
    pass


def parse_business_date(value: Any) -> date | None:
    """Parse only explicit, unambiguous business dates."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("年", "-").replace("月", "-").replace("日", "")
    match = re.fullmatch(
        r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"
        r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?",
        text,
    )
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def digest_row(values: dict[str, Any]) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def evidence_root(run_id: str) -> Path:
    root = Path(os.environ.get("BINHU_CLEANUP_EVIDENCE_ROOT", "/srv"))
    return (root / f"binhu-release-evidence-current-flow-cleanup-{run_id}").resolve()


def _json_dump(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def connect():
    return await aiomysql.connect(
        host=os.environ.get("MYSQL_HOST", "mysql"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "binhu"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        db="OnlineData",
        autocommit=False,
        charset="utf8mb4",
    )


async def query_one(cur, sql: str, params: tuple[Any, ...] = ()):
    await cur.execute(sql, params)
    return await cur.fetchone()


async def query_all(cur, sql: str, params: tuple[Any, ...] = ()):
    await cur.execute(sql, params)
    return await cur.fetchall()


async def table_exists(cur, schema: str, table: str) -> bool:
    row = await query_one(
        cur,
        "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
        (schema, table),
    )
    return bool(row)


async def load_target(cur, target_date: date) -> list[dict[str, Any]]:
    columns = ["id", "_row_key", *DATE_FIELDS]
    # Older installations may only expose 下发日期; inspect before selecting aliases.
    available = {
        str(row[0])
        for row in await query_all(
            cur,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=%s",
            (TABLE,),
        )
    }
    selected = [column for column in columns if column in {"id", "_row_key"} or column in available]
    rows = await query_all(
        cur,
        "SELECT " + ",".join(f"`{column}`" for column in selected) + f" FROM `{TABLE}`",
    )
    targets: list[dict[str, Any]] = []
    for raw in rows:
        values = dict(zip(selected, raw))
        business = next((parse_business_date(values.get(field)) for field in DATE_FIELDS if field in values), None)
        if business == target_date:
            targets.append({
                "id": int(values["id"]),
                "row_key": str(values.get("_row_key") or ""),
                "row_digest": digest_row(values),
            })
    return [item for item in targets if item["row_key"]]


async def counts(cur, row_keys: list[str], source_ids: list[int]) -> dict[str, int]:
    if not row_keys:
        return {"business": 0, "source": 0, "projection": 0, "address_match": 0, "local_source": 0, "review_flow": 0, "registration": 0, "graph_nodes": 0}
    placeholders = ",".join(["%s"] * len(row_keys))
    source_ph = ",".join(["%s"] * len(source_ids)) or "NULL"
    result: dict[str, int] = {}
    result["business"] = int((await query_one(cur, f"SELECT COUNT(*) FROM `{TABLE}` WHERE `_row_key` IN ({placeholders})", tuple(row_keys)))[0])
    result["source"] = int((await query_one(cur, f"SELECT COUNT(*) FROM `_online_source_rows` WHERE parser_type=%s AND row_key IN ({placeholders}) AND archived_at IS NULL", (PARSER_TYPE, *row_keys)))[0])
    result["projection"] = int((await query_one(cur, f"SELECT COUNT(*) FROM `_online_source_projection` WHERE parser_type=%s AND row_key IN ({placeholders})", (PARSER_TYPE, *row_keys)))[0])
    result["address_match"] = int((await query_one(cur, f"SELECT COUNT(*) FROM `_online_task_address_matches` WHERE parser_type=%s AND row_key IN ({placeholders})", (PARSER_TYPE, *row_keys)))[0])
    result["local_source"] = int((await query_one(cur, f"SELECT COUNT(*) FROM `_local_source_records` WHERE parser_type=%s AND business_key IN ({placeholders}) AND archived_at IS NULL", (PARSER_TYPE, *row_keys)))[0])
    result["review_flow"] = int((await query_one(cur, f"SELECT COUNT(*) FROM `_unverifiable_review_flows` WHERE parser_type=%s AND row_key IN ({placeholders}) AND archived_at IS NULL", (PARSER_TYPE, *row_keys)))[0])
    result["registration"] = int((await query_one(cur, f"SELECT COUNT(*) FROM `_task_registration_links` WHERE parser_type=%s AND row_key IN ({placeholders})", (PARSER_TYPE, *row_keys)))[0])
    workflow = os.environ.get("MYSQL_WORKFLOW_DB", "WorkflowData")
    if await table_exists(cur, workflow, "task_graph_nodes"):
        result["graph_nodes"] = int((await query_one(cur, f"SELECT COUNT(*) FROM `{workflow}`.`task_graph_nodes` WHERE provider='online' AND parser_type=%s AND source_ref IN ({placeholders}) AND archived_at IS NULL", (PARSER_TYPE, *row_keys)))[0])
    else:
        result["graph_nodes"] = 0
    return result


async def load_manifest(root: Path) -> dict[str, Any]:
    path = root / "target.json"
    if not path.is_file():
        raise CleanupError("target_manifest_missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CleanupError("target_manifest_invalid") from exc
    if payload.get("parser_type") != PARSER_TYPE or not SAFE_RUN.fullmatch(str(payload.get("run_id") or "")):
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
            valid = True
            for database in DATABASES:
                entry = entries[database]
                path = manifest_path.parent / str(entry.get("filename") or "")
                sha_path = Path(str(path) + ".sha256")
                if not path.is_file() or not sha_path.is_file() or path.stat().st_size <= 0:
                    valid = False
                    break
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                expected = str(entry.get("sha256") or "")
                recorded = sha_path.read_text(encoding="ascii").split()[0] if sha_path.read_text(encoding="ascii").split() else ""
                if digest != expected or digest != recorded:
                    valid = False
                    break
                files.append({"database": database, "size_bytes": path.stat().st_size, "sha256": digest})
            if valid:
                age_seconds = max(0, datetime.now(timezone.utc).timestamp() - manifest_path.stat().st_mtime)
                if age_seconds <= int(os.environ.get("BINHU_CLEANUP_MAX_BACKUP_AGE_SECONDS", "86400")):
                    return {"backup_id": manifest_path.parent.name, "manifest": str(manifest_path), "age_seconds": int(age_seconds), "files": files}
        except (OSError, ValueError, TypeError, IndexError):
            continue
    raise CleanupError("backup_missing_or_invalid")


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
                targets = await load_target(cur, business_date)
                row_keys = [item["row_key"] for item in targets]
                source_rows = await query_all(cur, "SELECT id FROM `_online_source_rows` WHERE parser_type=%s AND row_key IN (" + ",".join(["%s"] * len(row_keys) or ["NULL"]) + ") AND archived_at IS NULL", (PARSER_TYPE, *row_keys)) if row_keys else []
                payload = {"schema": 1, "run_id": run_id, "parser_type": PARSER_TYPE, "business_date": business_date.isoformat(), "targets": targets, "source_ids": [int(row[0]) for row in source_rows], "counts": await counts(cur, row_keys, [int(row[0]) for row in source_rows]), "measured_at": datetime.now(timezone.utc).isoformat()}
                _json_dump(root / "target.json", payload)
                all_rows = await query_all(cur, f"SELECT `下发日期` FROM `{TABLE}`")
                preserved_before = sum(1 for (raw_date,) in all_rows if (parsed := parse_business_date(raw_date)) and parsed < business_date)
                _json_dump(root / "measure.json", {"target_count": len(targets), "counts": payload["counts"], "preserved_before_date_count": preserved_before})
                return {"phase": phase, "target_count": len(targets), "counts": payload["counts"]}
            manifest = await load_manifest(root)
            if phase == "prepare":
                backup = check_backup_root(Path(os.environ.get("BINHU_DEPLOY_BACKUP_ROOT", "/root/binhu/deploy-backups/automated")))
                manifest["backup"] = backup
                _json_dump(root / "target.json", manifest)
                _json_dump(root / "prepare.json", {"ready": True, "backup_id": backup["backup_id"], "target_count": len(manifest.get("targets", []))})
                return {"phase": phase, "ready": True, "target_count": len(manifest.get("targets", []))}
            if phase == "verify":
                row_keys = [str(item["row_key"]) for item in manifest.get("targets", [])]
                source_ids = [int(item) for item in manifest.get("source_ids", [])]
                after = await counts(cur, row_keys, source_ids)
                remaining = int((await query_one(cur, f"SELECT COUNT(*) FROM `{TABLE}` WHERE `_row_key` IN ({','.join(['%s'] * len(row_keys) or ['NULL'])})", tuple(row_keys)))[0]) if row_keys else 0
                all_rows = await query_all(cur, f"SELECT `下发日期` FROM `{TABLE}`")
                old_count = sum(1 for (raw_date,) in all_rows if (parsed := parse_business_date(raw_date)) and parsed < business_date)
                measured_before = None
                if (root / "measure.json").is_file():
                    try:
                        measured_before = int(json.loads((root / "measure.json").read_text(encoding="utf-8")).get("preserved_before_date_count", -1))
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        measured_before = None
                payload = {"ready": remaining == 0 and measured_before is not None and old_count == measured_before, "remaining_target_business": remaining, "preserved_before_date_count": old_count, "after_counts": after}
                _json_dump(root / "verify.json", payload)
                if not payload["ready"]:
                    raise CleanupError("verification_failed")
                return {"phase": phase, **payload}
            if phase != "apply":
                raise CleanupError("unsupported_phase")
            if not (root / "prepare.json").is_file():
                raise CleanupError("prepare_required")
            row_keys = [str(item["row_key"]) for item in manifest.get("targets", [])]
            if not row_keys:
                await conn.commit()
                _json_dump(root / "apply.json", {"status": "success", "target_count": 0})
                return {"phase": phase, "status": "success", "target_count": 0}
            placeholders = ",".join(["%s"] * len(row_keys))
            await cur.execute("SELECT GET_LOCK('binhu-current-flow-cleanup', 10)")
            lock = await cur.fetchone()
            if not lock or int(lock[0]) != 1:
                raise CleanupError("maintenance_lock_unavailable")
            try:
                # Archive the exact current rows, then remove them from the live table.
                await cur.execute(f"INSERT INTO `OnlineDataArchive`.`{ARCHIVE_TABLE}` SELECT current.*, UTC_TIMESTAMP(), 'current_flow_cleanup_20260914' FROM `OnlineData`.`{TABLE}` current WHERE current.`_row_key` IN ({placeholders}) AND NOT EXISTS (SELECT 1 FROM `OnlineDataArchive`.`{ARCHIVE_TABLE}` archived WHERE archived.`_row_key`=current.`_row_key` AND archived.`_archive_reason`='current_flow_cleanup_20260914')", tuple(row_keys))
                await cur.execute(f"DELETE FROM `OnlineData`.`{TABLE}` WHERE `_row_key` IN ({placeholders})", tuple(row_keys))
                await cur.execute(f"UPDATE `_online_source_rows` SET archived_at=UTC_TIMESTAMP(), revision=revision+1, source_kind='current_flow_cleanup' WHERE parser_type=%s AND row_key IN ({placeholders}) AND archived_at IS NULL", (PARSER_TYPE, *row_keys))
                await cur.execute(f"UPDATE `_local_source_records` SET status='archived', archived_at=UTC_TIMESTAMP(), updated_at=UTC_TIMESTAMP() WHERE parser_type=%s AND business_key IN ({placeholders}) AND archived_at IS NULL", (PARSER_TYPE, *row_keys))
                for table in ("_online_source_projection", "_online_task_address_matches"):
                    await cur.execute(f"DELETE FROM `{table}` WHERE parser_type=%s AND row_key IN ({placeholders})", (PARSER_TYPE, *row_keys))
                await cur.execute(f"UPDATE `_online_local_changes` SET status='cancelled', error_code='current_flow_archived', updated_at=UTC_TIMESTAMP() WHERE parser_type=%s AND row_key IN ({placeholders}) AND status IN ('pending','processing','retry')", (PARSER_TYPE, *row_keys))
                await cur.execute(f"UPDATE `_online_projection_jobs` SET status='cancelled', error_code='current_flow_archived', updated_at=UTC_TIMESTAMP() WHERE parser_type=%s AND row_key IN ({placeholders}) AND status IN ('pending','queued','running','retry')", (PARSER_TYPE, *row_keys))
                await cur.execute(f"UPDATE `_online_summary_updates` SET status='cancelled', error_code='current_flow_archived', updated_at=UTC_TIMESTAMP() WHERE parser_type=%s AND row_key IN ({placeholders}) AND status IN ('pending','queued','running','retry')", (PARSER_TYPE, *row_keys))
                await cur.execute(f"UPDATE `_task_registration_links` SET status='cancelled', reason_code='source_missing', updated_at=UTC_TIMESTAMP() WHERE parser_type=%s AND row_key IN ({placeholders})", (PARSER_TYPE, *row_keys))
                await cur.execute(f"UPDATE `_unverifiable_review_flows` SET archived_at=UTC_TIMESTAMP(), updated_at=UTC_TIMESTAMP() WHERE parser_type=%s AND row_key IN ({placeholders}) AND archived_at IS NULL", (PARSER_TYPE, *row_keys))
                workflow = os.environ.get("MYSQL_WORKFLOW_DB", "WorkflowData")
                if await table_exists(cur, workflow, "task_graph_nodes"):
                    await cur.execute(f"UPDATE `{workflow}`.`task_graph_nodes` SET status='source_missing', reason_code='current_flow_archived', archived_at=UTC_TIMESTAMP(), updated_at=UTC_TIMESTAMP() WHERE provider='online' AND parser_type=%s AND source_ref IN ({placeholders}) AND archived_at IS NULL", (PARSER_TYPE, *row_keys))
                    await cur.execute(f"UPDATE `{workflow}`.`task_graph_dependencies` dependency JOIN `{workflow}`.`task_graph_nodes` predecessor ON predecessor.id=dependency.predecessor_node_id JOIN `{workflow}`.`task_graph_nodes` successor ON successor.id=dependency.successor_node_id SET dependency.state='cancelled', dependency.reason_code='current_flow_archived', dependency.cancelled_at=COALESCE(dependency.cancelled_at,UTC_TIMESTAMP()), dependency.updated_at=UTC_TIMESTAMP() WHERE dependency.state='active' AND ((predecessor.parser_type=%s AND predecessor.source_ref IN ({placeholders})) OR (successor.parser_type=%s AND successor.source_ref IN ({placeholders})))", (PARSER_TYPE, *row_keys, PARSER_TYPE, *row_keys))
                await conn.commit()
                _json_dump(root / "apply.json", {"status": "success", "target_count": len(row_keys), "archive_reason": "current_flow_cleanup_20260914"})
                return {"phase": phase, "status": "success", "target_count": len(row_keys)}
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
    parser.add_argument("--parser-type", default=PARSER_TYPE)
    args = parser.parse_args(argv)
    if args.parser_type != PARSER_TYPE or not SAFE_RUN.fullmatch(args.run_id):
        print("invalid cleanup scope", file=sys.stderr)
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
