"""Fixed, Staging-only database schema migration contract.

The migration runs the exact idempotent ``init_db`` entry point shipped in the
accepted Staging backend image.  The gateway never accepts SQL, database names,
paths, containers, or arbitrary commands from its caller.  Only table metadata
and SHA-256 signatures leave the backend container.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .artifact import file_hash, write_json
from .database_identity import run as verify_database_identity
from .runtime import root_for
from .update import EVIDENCE_ROOT, backup_databases, read_configuration


RUN_RE = re.compile(r"staging-migrate-[0-9a-f]{16}")
ARTIFACT_RE = re.compile(r"[0-9a-f]{64}")
BASE = EVIDENCE_ROOT / "staging-migrations"
DOMAINS = (
    "ONLINE_DATA", "ARCHIVE", "DAILY_REPORT", "PLATFORM",
    "VISIT", "DISPATCH", "REGISTRY", "WORKFLOW",
)


def _fixed(reason: str) -> None:
    raise ValueError(reason)


def _run_root(run_id: str) -> Path:
    if not RUN_RE.fullmatch(run_id):
        _fixed("staging_migration_run_id_invalid")
    root = BASE / run_id
    if root.parent != BASE or root.is_symlink():
        _fixed("staging_migration_evidence_path_invalid")
    return root


def _exclusive(path: Path, value: dict) -> None:
    if path.exists() or path.is_symlink():
        _fixed("staging_migration_evidence_exists")
    write_json(path, value)
    path.chmod(0o600)


def _staging_identity(artifact_id: str) -> tuple[Path, dict, dict, str]:
    if not ARTIFACT_RE.fullmatch(artifact_id):
        _fixed("staging_migration_artifact_id_invalid")
    root = root_for("staging")
    manifest, compose, env_text = read_configuration(root)
    if manifest.get("environment") != "staging" or manifest.get("artifact_id") != artifact_id:
        _fixed("staging_migration_artifact_identity_mismatch")
    if compose.get("name") != "binhu-staging" or "APP_ENVIRONMENT=staging" not in env_text:
        _fixed("staging_migration_environment_identity_mismatch")
    return root, manifest, compose, env_text


def _command(args: list[str], *, stdin: str | None = None, timeout: int = 90) -> str:
    result = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError("staging_migration_runtime_failed")
    return result.stdout


def _backend_container(compose: dict) -> str:
    # Compose names are read from the accepted manifest, but the container id
    # is obtained from Docker metadata rather than caller-controlled input.
    expected = compose["name"] + "-backend-1"
    ids = _command(["docker", "ps", "-aq"]).split()
    if not ids:
        _fixed("staging_migration_backend_missing")
    containers = json.loads(_command(["docker", "inspect", *ids]))
    for item in containers:
        if item.get("Name") == "/" + expected:
            labels = item.get("Config", {}).get("Labels") or {}
            if (labels.get("com.docker.compose.project") != "binhu-staging"
                    or labels.get("com.docker.compose.service") != "backend"
                    or labels.get("binhu.environment") != "staging"
                    or not item.get("State", {}).get("Running")):
                _fixed("staging_migration_backend_identity_mismatch")
            return item["Id"]
    _fixed("staging_migration_backend_missing")


INNER = r'''
import asyncio, hashlib, json
import aiomysql
from config import settings
from database import init_db, close_db

DOMAIN_NAMES = ("ONLINE_DATA", "ARCHIVE", "DAILY_REPORT", "PLATFORM", "VISIT", "DISPATCH", "REGISTRY", "WORKFLOW")
PREFIX = "Staging_"

async def signature(cur, database, table):
    await cur.execute("SELECT column_name,column_type,is_nullable,column_default,extra,generation_expression FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (database, table))
    columns = [list(row) for row in await cur.fetchall()]
    await cur.execute("SELECT index_name,non_unique,seq_in_index,column_name,sub_part,index_type FROM information_schema.statistics WHERE table_schema=%s AND table_name=%s ORDER BY index_name,seq_in_index", (database, table))
    indexes = [list(row) for row in await cur.fetchall()]
    await cur.execute("SELECT constraint_name,constraint_type FROM information_schema.table_constraints WHERE table_schema=%s AND table_name=%s ORDER BY constraint_name", (database, table))
    constraints = [list(row) for row in await cur.fetchall()]
    value = {"columns": columns, "indexes": indexes, "constraints": constraints}
    return value, hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

async def inventory():
    if settings.APP_ENVIRONMENT != "staging" or settings.MYSQL_HOST != "environment-mysql" or settings.MYSQL_USER != "environment_app":
        raise ValueError("staging_migration_inner_target_mismatch")
    names = [getattr(settings, "MYSQL_" + name + "_DB") for name in DOMAIN_NAMES]
    if len(set(names)) != 8 or any(not name.startswith(PREFIX) for name in names):
        raise ValueError("staging_migration_inner_database_mismatch")
    conn = await aiomysql.connect(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER, password=settings.MYSQL_PASSWORD, connect_timeout=5, autocommit=False)
    try:
        result_domains = {}
        for key, database in zip(DOMAIN_NAMES, names):
            async with conn.cursor() as cur:
                await cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name", (database,))
                tables = [row[0] for row in await cur.fetchall()]
                signatures = {}
                for table in tables:
                    signatures[table] = (await signature(cur, database, table))[1]
                result_domains[key] = {"database": database, "tables": tables, "signatures": signatures}
        payload = {"domains": result_domains}
        payload["schema_hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        return payload
    finally:
        conn.close()

async def migrate():
    await init_db()
    await close_db()

async def main():
    before = await inventory()
    if APPLY:
        await migrate()
    after = await inventory()
    print(json.dumps({"before": before, "after": after}, ensure_ascii=True, separators=(",", ":")))

APPLY = APPLY_VALUE
asyncio.run(main())
'''


def _run_inner(container: str, *, apply: bool) -> dict:
    source = INNER.replace("APPLY_VALUE", repr(bool(apply)), 1)
    output = _command(["docker", "exec", "-i", container, "python", "-"], stdin=source, timeout=600)
    lines = [line for line in output.splitlines() if line.startswith("{")]
    if len(lines) != 1:
        _fixed("staging_migration_runtime_output_invalid")
    try:
        result = json.loads(lines[0])
    except json.JSONDecodeError:
        _fixed("staging_migration_runtime_output_invalid")
    if not isinstance(result.get("before"), dict) or not isinstance(result.get("after"), dict):
        _fixed("staging_migration_runtime_output_invalid")
    return result


def _common(run_id: str, artifact_id: str, action: str) -> dict:
    root, manifest, compose, env_text = _staging_identity(artifact_id)
    verify_database_identity("staging", apply=False)
    return {"environment": "staging", "run_id": run_id, "artifact_id": artifact_id,
            "commit": manifest.get("commit"), "version": manifest.get("version"),
            "action": action, "configuration_hashes": manifest.get("hashes", {}),
            "database_backup": False, "production_modified": False}


def measure(run_id: str, artifact_id: str) -> dict:
    root = _run_root(run_id)
    if root.exists() or root.is_symlink():
        _fixed("staging_migration_evidence_exists")
    root.mkdir(mode=0o700, parents=True)
    try:
        report = _common(run_id, artifact_id, "measure")
        _, _, compose, _ = _staging_identity(artifact_id)
        result = _run_inner(_backend_container(compose), apply=False)
        report.update({"before": result["before"], "after": result["after"],
                       "schema_hash": result["after"]["schema_hash"], "state": "measured"})
        _exclusive(root / "measure.json", report)
        return report
    except Exception as exc:
        reason = str(exc) if re.fullmatch(r"[a-z0-9_]{1,100}", str(exc)) else "staging_migration_measure_failed"
        _exclusive(root / "failure.json", {"environment": "staging", "run_id": run_id,
                                             "action": "measure", "reason": reason,
                                             "evidence_preserved": True})
        raise


def apply(run_id: str, artifact_id: str) -> dict:
    root = _run_root(run_id)
    measure_path = root / "measure.json"
    if not measure_path.is_file() or measure_path.is_symlink():
        _fixed("staging_migration_measure_required")
    report = json.loads(measure_path.read_text(encoding="utf-8"))
    if report.get("artifact_id") != artifact_id or report.get("state") != "measured":
        _fixed("staging_migration_measure_identity_invalid")
    try:
        root_config, manifest, compose, _ = _staging_identity(artifact_id)
        backup_databases("staging", root_config, manifest, root)
        result = _run_inner(_backend_container(compose), apply=True)
        applied = {**report, "action": "migrate_apply", "before": result["before"],
                   "after": result["after"], "schema_hash": result["after"]["schema_hash"],
                   "database_backup": True, "state": "applied"}
        _exclusive(root / "apply.json", applied)
        return applied
    except Exception as exc:
        reason = str(exc) if re.fullmatch(r"[a-z0-9_]{1,100}", str(exc)) else "staging_migration_apply_failed"
        _exclusive(root / "failure.json", {"environment": "staging", "run_id": run_id,
                                             "action": "migrate_apply", "reason": reason,
                                             "database_restored": False, "evidence_preserved": True})
        raise


def verify(run_id: str, artifact_id: str) -> dict:
    root = _run_root(run_id)
    apply_path = root / "apply.json"
    if not apply_path.is_file() or apply_path.is_symlink():
        _fixed("staging_migration_apply_required")
    applied = json.loads(apply_path.read_text(encoding="utf-8"))
    if applied.get("artifact_id") != artifact_id or applied.get("state") != "applied":
        _fixed("staging_migration_apply_identity_invalid")
    try:
        _, _, compose, _ = _staging_identity(artifact_id)
        result = _run_inner(_backend_container(compose), apply=False)
        expected = applied.get("schema_hash")
        actual = result["after"]["schema_hash"]
        if expected != actual:
            _fixed("staging_migration_schema_changed_after_apply")
        report = {"environment": "staging", "run_id": run_id, "artifact_id": artifact_id,
                  "commit": applied.get("commit"), "version": applied.get("version"),
                  "before_schema_hash": applied["before"]["schema_hash"],
                  "after_schema_hash": actual, "schema_hash_matches": True,
                  "database_backup": applied.get("database_backup") is True,
                  "verified": True, "production_modified": False, "state": "verified"}
        _exclusive(root / "verify.json", report)
        return report
    except Exception as exc:
        reason = str(exc) if re.fullmatch(r"[a-z0-9_]{1,100}", str(exc)) else "staging_migration_verify_failed"
        _exclusive(root / "failure.json", {"environment": "staging", "run_id": run_id,
                                             "action": "verify", "reason": reason,
                                             "evidence_preserved": True})
        raise


def status() -> dict:
    runs = []
    if BASE.is_dir() and not BASE.is_symlink():
        for path in sorted(BASE.iterdir()):
            if RUN_RE.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
                runs.append({"run_id": path.name,
                             "completed": [name for name in ("measure", "apply", "verify")
                                           if (path / (name + ".json")).is_file()]})
    return {"gateway": "staging-migration", "environment": "staging", "runs": runs[-10:]}
