"""Fixed, Staging-only schema migration for the measured application candidate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess

from .artifact import verify_artifact, write_json
from .database_identity import run as verify_database_identity
from .image import verify_image
from .runtime import root_for
from .update import EVIDENCE_ROOT, backup_databases, read_configuration


RUN_RE = re.compile(r"staging-migrate-[0-9a-f]{16}")
ARTIFACT_RE = re.compile(r"[0-9a-f]{64}")
BASE = EVIDENCE_ROOT / "staging-migrations"
APPLICATION_ROOT = Path("/var/lib/binhu-staging-application")
MIGRATION_MODULES = (
    "migrations.task_command_read_model",
    "migrations.task_view_state_revisions",
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


def _staging_runtime() -> tuple[Path, dict, dict, str]:
    root = root_for("staging")
    manifest, compose, env_text = read_configuration(root)
    if manifest.get("environment") != "staging" or compose.get("name") != "binhu-staging":
        _fixed("staging_migration_environment_identity_mismatch")
    if "APP_ENVIRONMENT=staging" not in env_text:
        _fixed("staging_migration_environment_identity_mismatch")
    verify_database_identity("staging", apply=False)
    return root, manifest, compose, env_text


def _candidate(artifact_id: str) -> tuple[Path, dict, dict]:
    if not ARTIFACT_RE.fullmatch(artifact_id):
        _fixed("staging_migration_artifact_id_invalid")
    candidates = APPLICATION_ROOT / "candidates"
    if candidates.is_symlink() or not candidates.is_dir():
        _fixed("staging_migration_candidate_missing")
    matches = []
    for root in candidates.glob("staging-app-*"):
        if root.is_symlink() or not root.is_dir():
            continue
        path = root / "promotion.json"
        if path.is_symlink() or not path.is_file():
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if manifest.get("artifact_id") == artifact_id:
            matches.append((root, manifest))
    if len(matches) != 1:
        _fixed("staging_migration_candidate_missing" if not matches
               else "staging_migration_candidate_ambiguous")
    root, manifest = matches[0]
    if (manifest.get("environment") != "staging"
            or manifest.get("state") != "measured"
            or manifest.get("run_id") != root.name
            or not re.fullmatch(r"staging-app-[0-9a-f]{16}", root.name)
            or not ARTIFACT_RE.fullmatch(str(manifest.get("artifact_id", "")))
            or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("commit", "")))
            or not re.fullmatch(r"\d+\.\d+\.\d+", str(manifest.get("version", "")))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(manifest.get("image_id", "")))
            or not re.fullmatch(r"dev-update-[0-9a-f]{16}",
                                str(manifest.get("dev_acceptance_run_id", "")))):
        _fixed("staging_migration_candidate_identity_mismatch")
    try:
        artifact = verify_artifact(root / "artifact")
        image = verify_image(root / "artifact", artifact_id, root / "image")
    except (OSError, ValueError, RuntimeError):
        _fixed("staging_migration_candidate_identity_mismatch")
    if (artifact.get("commit") != manifest.get("commit")
            or artifact.get("version") != manifest.get("version")
            or image.get("image_id") != manifest.get("image_id")):
        _fixed("staging_migration_candidate_identity_mismatch")
    dev_path = EVIDENCE_ROOT / manifest["dev_acceptance_run_id"] / "result.json"
    if dev_path.is_symlink() or not dev_path.is_file():
        _fixed("staging_migration_dev_acceptance_missing")
    try:
        dev_report = json.loads(dev_path.read_text(encoding="utf-8"))
        dev_root = root_for("development")
        dev_manifest, dev_compose, _ = read_configuration(dev_root)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _fixed("staging_migration_dev_acceptance_invalid")
    if (dev_report.get("environment") != "development" or dev_report.get("health") is not True
            or dev_report.get("artifact_id") != artifact_id
            or dev_report.get("image_id") != image["image_id"]
            or dev_report.get("commit") != manifest["commit"]
            or dev_report.get("version") != manifest["version"]
            or dev_manifest.get("environment") != "development"
            or dev_manifest.get("artifact_id") != artifact_id
            or dev_manifest.get("images", {}).get("backend") != image["image_id"]
            or dev_manifest.get("commit") != manifest["commit"]
            or dev_manifest.get("version") != manifest["version"]
            or dev_compose.get("name") != "binhu-development"):
        _fixed("staging_migration_dev_acceptance_invalid")
    return root, manifest, image


def _command(args: list[str], *, timeout: int = 90) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError("staging_migration_runtime_failed")
    return result.stdout


def _migration_output(root: Path, compose: dict, env_text: str, image_id: str,
                      module: str, phase: str, version: str) -> dict:
    if module not in MIGRATION_MODULES or phase not in {"measure", "apply", "verify"}:
        _fixed("staging_migration_operation_invalid")
    network = compose.get("networks", {}).get("internal", {}).get("name")
    if network != "binhu-staging_internal":
        _fixed("staging_migration_network_identity_mismatch")
    network_info = json.loads(_command(["docker", "network", "inspect", network]))
    if (not isinstance(network_info, list) or len(network_info) != 1
            or (network_info[0].get("Labels") or {}).get("binhu.environment") != "staging"
            or (network_info[0].get("Labels") or {}).get("com.docker.compose.project")
            != "binhu-staging"):
        _fixed("staging_migration_network_identity_mismatch")
    if "APP_ENVIRONMENT=staging" not in env_text:
        _fixed("staging_migration_environment_identity_mismatch")
    config_file = root / "backend.env"
    if config_file.is_symlink() or not config_file.is_file():
        _fixed("staging_migration_environment_identity_mismatch")
    if phase == "measure":
        args = ["measure"]
    elif phase == "apply":
        args = ["migrate", "--apply"]
    else:
        args = ["verify"]
    output = _command([
        "docker", "run", "--rm", "--network", network,
        "--env-file", str(config_file), "--env", "APP_VERSION=" + version,
        "--read-only", "--cap-drop", "ALL", "--security-opt",
        "no-new-privileges:true", "--memory", "512m", "--cpus", "1",
        "--pids-limit", "128", "--workdir", "/app", "--entrypoint", "python",
        image_id, "-B", "-m", module, *args,
    ], timeout=600)
    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        _fixed("staging_migration_runtime_output_invalid")
    expected_command = "measure" if phase == "measure" else "migrate" if phase == "apply" else "verify"
    if not isinstance(result, dict) or result.get("command") != expected_command:
        _fixed("staging_migration_runtime_output_invalid")
    if phase == "apply" and result.get("apply") is not True:
        _fixed("staging_migration_runtime_output_invalid")
    return result


def _run_modules(root: Path, compose: dict, env_text: str, image_id: str,
                 phase: str, version: str) -> dict:
    return {module: _migration_output(root, compose, env_text, image_id,
                                      module, phase, version)
            for module in MIGRATION_MODULES}


def _json_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def _common(run_id: str, artifact_id: str, action: str, manifest: dict,
            image_id: str) -> dict:
    return {"environment": "staging", "run_id": run_id, "artifact_id": artifact_id,
            "commit": manifest["commit"], "version": manifest["version"],
            "candidate_run_id": manifest["run_id"],
            "image_id": image_id,
            "action": action,
            "database_backup": False, "production_modified": False}


def measure(run_id: str, artifact_id: str) -> dict:
    evidence_root = _run_root(run_id)
    if evidence_root.exists() or evidence_root.is_symlink():
        _fixed("staging_migration_evidence_exists")
    evidence_root.mkdir(mode=0o700, parents=True)
    try:
        runtime_root, _, compose, env_text = _staging_runtime()
        _, candidate_manifest, image = _candidate(artifact_id)
        report = _common(run_id, artifact_id, "measure", candidate_manifest, image["image_id"])
        results = _run_modules(runtime_root, compose, env_text, image["image_id"],
                               "measure", candidate_manifest["version"])
        measurement_hash = _json_hash(results)
        report.update({"modules": results, "measurement_hash": measurement_hash,
                       "state": "measured"})
        _exclusive(evidence_root / "measure.json", report)
        return report
    except Exception as exc:
        reason = str(exc) if re.fullmatch(r"[a-z0-9_]{1,100}", str(exc)) else "staging_migration_measure_failed"
        _exclusive(evidence_root / "failure.json", {"environment": "staging", "run_id": run_id,
                                             "action": "measure", "reason": reason,
                                             "evidence_preserved": True})
        raise


def apply(run_id: str, artifact_id: str) -> dict:
    root = _run_root(run_id)
    measure_path = root / "measure.json"
    if not measure_path.is_file() or measure_path.is_symlink():
        _fixed("staging_migration_measure_required")
    report = json.loads(measure_path.read_text(encoding="utf-8"))
    _, candidate_manifest, image = _candidate(artifact_id)
    if (report.get("artifact_id") != artifact_id or report.get("state") != "measured"
            or report.get("candidate_run_id") != candidate_manifest.get("run_id")
            or report.get("image_id") != image.get("image_id")):
        _fixed("staging_migration_measure_identity_invalid")
    try:
        root_config, app_manifest, compose, env_text = _staging_runtime()
        current_measurement = _run_modules(root_config, compose, env_text, image["image_id"],
                                           "measure", candidate_manifest["version"])
        if _json_hash(current_measurement) != report.get("measurement_hash"):
            _fixed("staging_migration_schema_changed_after_measure")
        backup_databases("staging", root_config, app_manifest, root)
        applied_modules = _run_modules(root_config, compose, env_text, image["image_id"],
                                        "apply", candidate_manifest["version"])
        if not all(item.get("consistent") is True for item in applied_modules.values()):
            _fixed("staging_migration_apply_inconsistent")
        applied = {**report, "action": "migrate_apply", "modules": applied_modules,
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
    evidence_root = _run_root(run_id)
    apply_path = evidence_root / "apply.json"
    if not apply_path.is_file() or apply_path.is_symlink():
        _fixed("staging_migration_apply_required")
    applied = json.loads(apply_path.read_text(encoding="utf-8"))
    if applied.get("artifact_id") != artifact_id or applied.get("state") != "applied":
        _fixed("staging_migration_apply_identity_invalid")
    try:
        _, candidate_manifest, image = _candidate(artifact_id)
        if (applied.get("candidate_run_id") != candidate_manifest.get("run_id")
                or applied.get("image_id") != image.get("image_id")):
            _fixed("staging_migration_apply_identity_invalid")
        runtime_root, _, compose, env_text = _staging_runtime()
        results = _run_modules(runtime_root, compose, env_text, image["image_id"],
                               "verify", candidate_manifest["version"])
        if not all(item.get("consistent") is True for item in results.values()):
            _fixed("staging_migration_verify_inconsistent")
        actual = _json_hash(results)
        report = {"environment": "staging", "run_id": run_id, "artifact_id": artifact_id,
                  "commit": applied.get("commit"), "version": applied.get("version"),
                  "candidate_run_id": applied.get("candidate_run_id"),
                  "image_id": applied.get("image_id"),
                  "measurement_hash": applied.get("measurement_hash"),
                  "verification_hash": actual, "modules": results,
                  "database_backup": applied.get("database_backup") is True,
                  "verified": True, "production_modified": False, "state": "verified"}
        _exclusive(evidence_root / "verify.json", report)
        return report
    except Exception as exc:
        reason = str(exc) if re.fullmatch(r"[a-z0-9_]{1,100}", str(exc)) else "staging_migration_verify_failed"
        _exclusive(evidence_root / "failure.json", {"environment": "staging", "run_id": run_id,
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
