"""Fixed Dev-only seed for the function-module prerequisite department.

This is deliberately separate from application startup. It can create or
reactivate only the standard ``社区警务队`` unit in ``Dev_PlatformData``.
No caller supplied database name, SQL, or shell command is accepted.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from gzip import open as gzip_open
from pathlib import Path


EVIDENCE_ROOT = Path("/srv/deploy-backups/environment-triad")
RUN_RE = re.compile(r"dev-department-[0-9a-f]{16}")
MYSQL_CONTAINER = "binhu-development-environment-mysql-1"
PLATFORM_DATABASE = "Dev_PlatformData"
TARGET_NAME = "社区警务队"
TARGET_TYPE = "unit"


def _evidence(run_id: str) -> Path:
    if not RUN_RE.fullmatch(run_id):
        raise ValueError("development_department_run_id_invalid")
    path = EVIDENCE_ROOT / run_id
    if path.parent != EVIDENCE_ROOT or path.is_symlink():
        raise ValueError("development_department_evidence_path_invalid")
    return path


def _docker_inspect() -> None:
    result = subprocess.run(
        ["docker", "inspect", MYSQL_CONTAINER, "--format",
         "{{index .Config.Labels \"com.docker.compose.project\"}}|"
         "{{index .Config.Labels \"com.docker.compose.service\"}}|"
         "{{index .Config.Labels \"binhu.environment\"}}"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode or result.stdout.strip() != "binhu-development|environment-mysql|development":
        raise RuntimeError("development_department_container_identity_invalid")


def _mysql(sql: str) -> list[str]:
    _docker_inspect()
    result = subprocess.run(
        ["docker", "exec", MYSQL_CONTAINER, "sh", "-c",
         'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql -uroot '
         "--default-character-set=utf8mb4 --batch --skip-column-names -e \"$1\"", "sh", sql],
        capture_output=True, text=True, timeout=90,
    )
    if result.returncode:
        raise RuntimeError("development_department_mysql_failed")
    return [line for line in result.stdout.splitlines() if line]


def _state() -> dict[str, object]:
    rows = _mysql(
        "SELECT id,name,department_type,IFNULL(community_id,''),"
        "IFNULL(parent_id,''),IFNULL(area_id,''),is_active "
        "FROM Dev_PlatformData._departments ORDER BY id"
    )
    fingerprint = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    target = [row.split("\t") for row in rows if len(row.split("\t")) >= 7
              and row.split("\t")[1] == TARGET_NAME]
    marker = _mysql(
        "SELECT config_value FROM Dev_PlatformData._system_config "
        "WHERE config_key='function_module_units_seeded'"
    )
    modules = _mysql(
        "SELECT module_id FROM Dev_PlatformData._function_modules "
        "ORDER BY module_id"
    )
    units = _mysql(
        "SELECT module_id FROM Dev_PlatformData._function_module_units "
        "WHERE module_id IN ('community_police','workflow') ORDER BY module_id"
    )
    return {
        "department_count": len(rows),
        "department_fingerprint": fingerprint,
        "target_count": len(target),
        "target_active_count": sum(item[6] == "1" for item in target),
        "target_types": sorted({item[2] for item in target}),
        "module_seed_marker": marker[0] if marker else None,
        "module_ids": modules,
        "module_unit_ids": units,
    }


def _write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def measure(run_id: str) -> dict[str, object]:
    path = _evidence(run_id)
    if path.exists():
        raise ValueError("development_department_evidence_exists")
    path.mkdir(mode=0o700, parents=True)
    try:
        state = _state()
        if state["target_count"] > 1 or (
            state["target_count"] == 1 and state["target_types"] != [TARGET_TYPE]
        ):
            raise ValueError("development_department_target_conflict")
        result = {
            "schema": 1,
            "environment": "development",
            "run_id": run_id,
            "operation": "seed-function-module-department",
            "target_name": TARGET_NAME,
            "target_type": TARGET_TYPE,
            "before": state,
            "expected_change": "create_or_activate_target_only",
            "module_seed_status": "pending_application_bootstrap",
            "measured_at": int(time.time()),
        }
        _write(path / "measure.json", result)
        return result
    except Exception as error:
        _write(path / "measure-failure.json", {
            "environment": "development", "run_id": run_id,
            "reason": str(error).split(";", 1)[0],
            "error_type": type(error).__name__,
        })
        raise


def apply(run_id: str) -> dict[str, object]:
    path = _evidence(run_id)
    measure_path = path / "measure.json"
    if not measure_path.is_file():
        raise ValueError("development_department_measure_required")
    before = json.loads(measure_path.read_text(encoding="utf-8"))["before"]
    backup = path / "platform-data.sql.gz"
    if backup.exists():
        raise ValueError("development_department_backup_exists")
    _docker_inspect()
    dump = subprocess.Popen(
        ["docker", "exec", MYSQL_CONTAINER, "sh", "-c",
         'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysqldump '
         "--single-transaction --routines --triggers --events --hex-blob "
         "--set-gtid-purged=OFF --databases Dev_PlatformData"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert dump.stdout is not None
    with gzip_open(backup, "wb") as destination:
        while True:
            chunk = dump.stdout.read(1024 * 1024)
            if not chunk:
                break
            destination.write(chunk)
    stderr = dump.stderr.read() if dump.stderr else b""
    status = dump.wait(timeout=300)
    if status:
        backup.unlink(missing_ok=True)
        raise RuntimeError("development_department_backup_failed")
    if backup.stat().st_size < 1024:
        backup.unlink(missing_ok=True)
        raise RuntimeError("development_department_backup_empty")

    sql = (
        "START TRANSACTION; "
        "INSERT INTO Dev_PlatformData._departments "
        "(name,department_type,community_id,parent_id,area_id,is_active) "
        "SELECT '社区警务队','unit',NULL,NULL,NULL,1 "
        "WHERE NOT EXISTS (SELECT 1 FROM Dev_PlatformData._departments WHERE name='社区警务队'); "
        "UPDATE Dev_PlatformData._departments SET department_type='unit',is_active=1 "
        "WHERE name='社区警务队' AND department_type='unit'; COMMIT;"
    )
    _mysql(sql)
    after = _state()
    if after["target_count"] != 1 or after["target_active_count"] != 1 or after["target_types"] != [TARGET_TYPE]:
        raise RuntimeError("development_department_apply_verification_failed")
    result = {
        "schema": 1, "environment": "development", "run_id": run_id,
        "operation": "seed-function-module-department", "before": before,
        "after": after, "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "backup_bytes": backup.stat().st_size, "stderr_empty": not bool(stderr),
        "applied_at": int(time.time()),
    }
    _write(path / "apply.json", result)
    return result


def verify(run_id: str) -> dict[str, object]:
    path = _evidence(run_id)
    if not (path / "apply.json").is_file():
        raise ValueError("development_department_apply_required")
    state = _state()
    if state["target_count"] != 1 or state["target_active_count"] != 1 or state["target_types"] != [TARGET_TYPE]:
        raise RuntimeError("development_department_verify_failed")
    module_seed_complete = (
        state["module_seed_marker"] == "1"
        and {"common", "community_police", "workflow", "platform_admin"}.issubset(set(state["module_ids"]))
        and {"community_police", "workflow"}.issubset(set(state["module_unit_ids"]))
    )
    result = {
        "schema": 1, "environment": "development", "run_id": run_id,
        "target_verified": True, "module_seed_complete": module_seed_complete,
        "module_seed_status": "complete" if module_seed_complete else "pending_application_bootstrap",
        "state": state, "verified_at": int(time.time()),
    }
    _write(path / "verify.json", result)
    return result
