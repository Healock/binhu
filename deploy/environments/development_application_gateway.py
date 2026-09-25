"""Fixed Dev application promotion gateway.

This gateway accepts an immutable Dev application artifact and only permits fixed development promotion operations.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import time

from .artifact import verify_artifact, write_json
from .image import build_image, verify_image
from .runtime import root_for
from .update import health, measure_development, apply_development, read_configuration, reconcile_development


BASE = Path("/var/lib/binhu-dev-application")
AUDIT_ROOT = Path("/var/log/binhu-dev-application-gateway")
EVIDENCE_ROOT = Path("/srv/deploy-backups/environment-triad")
RUN_RE = re.compile(r"dev-update-([0-9a-f]{16})")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
VERSION_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
ARTIFACT_RE = re.compile(r"[0-9a-f]{64}")
IMAGE_RE = re.compile(r"sha256:[0-9a-f]{64}")
ARCHIVE_MEMBERS = {"artifact.json", "source.tar", "frontend.tar"}
OPERATION_FAILURE_CODES = {
    "environment_configuration_drift",
    "environment_configuration_hashes_missing",
    "environment_external_access_enabled",
    "environment_image_drift",
    "environment_identity_mismatch",
    "environment_database_mismatch",
    "environment_database_identity_incomplete",
    "environment_services_unreviewed",
    "environment_network_mismatch",
    "environment_volume_mismatch",
    "environment_service_isolation_invalid",
    "environment_backend_override_invalid",
    "environment_static_mount_unreviewed",
    "environment_update_resources_insufficient",
}
CONTROL_COMMIT = Path('/usr/local/libexec/binhu-dev-application/control-commit')


def refuse(message: str = "fixed Dev application command required") -> None:
    raise ValueError(message)


def _safe_root(path: Path, *, create: bool = False) -> Path:
    if not path.is_absolute() or path != path.resolve() or any(item.is_symlink() for item in (path, *path.parents)):
        refuse("Dev application path identity invalid")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        refuse("Dev application path permissions invalid")
    return path


def _audit(action: str, *, run_id: str = "", outcome: str, details: dict | None = None) -> None:
    _safe_root(AUDIT_ROOT, create=True)
    allowed: dict[str, object] = {}
    for key, value in (details or {}).items():
        if key in {"artifact_id", "commit", "version", "image_id", "dev_acceptance_run_id", "reason"}:
            if isinstance(value, str) and len(value) <= 128 and re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
                allowed[key] = value
    entry = {"timestamp": int(time.time()), "gateway": "dev-application", "action": action,
             "run_id": run_id, "outcome": outcome, **allowed}
    path = AUDIT_ROOT / "application-audit.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _alert(action: str, run_id: str, reason: str) -> None:
    _safe_root(AUDIT_ROOT, create=True)
    path = AUDIT_ROOT / f"application-alert-{run_id}-{int(time.time())}.json"
    write_json(path, {"timestamp": int(time.time()), "gateway": "dev-application",
                      "action": action, "run_id": run_id, "severity": "error", "reason": reason})
    path.chmod(0o600)


def _failure_code(error: BaseException) -> str:
    """Return a stable, non-sensitive diagnostic code for private evidence."""
    if isinstance(error, NameError):
        return "gateway_missing_symbol"
    if isinstance(error, ValueError):
        return "candidate_validation_failed"
    if isinstance(error, RuntimeError):
        return "candidate_runtime_failed"
    if isinstance(error, (OSError, tarfile.TarError)):
        return "candidate_io_failed"
    return "candidate_prepare_failed"


def _operation_failure_code(error: BaseException) -> str:
    """Keep private operation evidence specific without exposing exception text."""
    message = str(error).split(";", 1)[0].strip()
    if message in OPERATION_FAILURE_CODES:
        return message
    return _failure_code(error)


def _run_root(run_id: str) -> Path:
    if not RUN_RE.fullmatch(run_id):
        refuse("fixed Dev application run id required")
    root = BASE / "candidates" / run_id
    if root.parent != BASE / "candidates" or root.is_symlink():
        refuse("Dev application candidate path invalid")
    return root


def _read_manifest(run_id: str) -> tuple[Path, dict]:
    root = _run_root(run_id)
    _safe_root(root)
    path = root / "promotion.json"
    if path.is_symlink() or not path.is_file():
        refuse("Dev application promotion manifest missing")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    required = {"schema", "environment", "run_id", "artifact_id", "image_id", "commit", "version", "prepared_at", "state"}
    if (set(manifest) != required or manifest.get("schema") != 1 or manifest.get("environment") != "development"
            or manifest.get("run_id") != run_id or not ARTIFACT_RE.fullmatch(str(manifest.get("artifact_id", "")))
            or not IMAGE_RE.fullmatch(str(manifest.get("image_id", "")))
            or not COMMIT_RE.fullmatch(str(manifest.get("commit", "")))
            or not VERSION_RE.fullmatch(str(manifest.get("version", "")))
            or manifest.get("state") not in {"prepared", "measured", "applied", "accepted"}):
        refuse("Dev application promotion manifest invalid")
    artifact, image = root / "artifact", root / "image"
    actual = verify_artifact(artifact)
    verified = verify_image(artifact, manifest["artifact_id"], image)
    if (actual["artifact_id"] != manifest["artifact_id"] or actual["commit"] != manifest["commit"]
            or actual["version"] != manifest["version"] or verified["image_id"] != manifest["image_id"]):
        refuse("Dev application immutable identity changed")
    return root, manifest


def _replace_manifest(root: Path, manifest: dict) -> None:
    """Atomically persist the fixed Dev promotion state without overwriting a pending file."""
    target = root / "promotion.json"
    temporary = root / "promotion.json.candidate"
    if temporary.exists() or temporary.is_symlink():
        refuse("Dev application manifest update unresolved")
    write_json(temporary, manifest)
    temporary.chmod(0o600)
    os.replace(temporary, target)


def _replace_json(path: Path, value: dict) -> None:
    """Atomically replace a mutable state report while preserving file permissions."""
    temporary = path.with_name(path.name + ".candidate")
    if temporary.exists() or temporary.is_symlink():
        refuse("Dev application report update unresolved")
    write_json(temporary, value)
    os.replace(temporary, path)


def _extract_artifact(data: bytes, target: Path) -> None:
    """Extract only the fixed three-file transport envelope into a new directory."""
    if len(data) > 256 * 1024 * 1024:
        refuse("Dev application artifact too large")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            names = {PurePosixPath(item.name).as_posix() for item in members}
            if (names != ARCHIVE_MEMBERS
                    or any(not item.isfile() or item.issym() or item.islnk() for item in members)):
                refuse("Dev application artifact contents invalid")
            for item in members:
                if item.size < 1 or item.size > 192 * 1024 * 1024:
                    refuse("Dev application artifact member invalid")
            archive.extractall(target, filter="data")
    except (tarfile.TarError, OSError):
        refuse("Dev application artifact archive invalid")

def prepare(run_id: str, commit: str, version: str, artifact_id: str, stream=None) -> dict:
    if (not COMMIT_RE.fullmatch(commit) or not VERSION_RE.fullmatch(version)
            or not ARTIFACT_RE.fullmatch(artifact_id) or not RUN_RE.fullmatch(run_id)):
        refuse("immutable Dev application identity required")
    if (CONTROL_COMMIT.is_symlink() or not CONTROL_COMMIT.is_file()
            or CONTROL_COMMIT.read_text(encoding="ascii").strip() != commit):
        refuse("candidate commit is not the independently installed main revision")
    _safe_root(BASE, create=True)
    _safe_root(BASE / "candidates", create=True)
    root = _run_root(run_id)
    if root.exists() or root.is_symlink():
        refuse("Dev application candidate already exists")
    root.mkdir(mode=0o700)
    artifact = root / "artifact"
    artifact.mkdir(mode=0o700)
    try:
        data = (stream or sys.stdin.buffer).read(256 * 1024 * 1024 + 1)
        _extract_artifact(data, artifact)
        candidate = verify_artifact(artifact)
        if (candidate["commit"] != commit or candidate["version"] != version
                or candidate["artifact_id"] != artifact_id):
            refuse("Dev application artifact is not the requested immutable candidate")
        image = build_image(artifact, artifact_id, root / "image")
        manifest = {"schema": 1, "environment": "development", "run_id": run_id,
                    "artifact_id": artifact_id, "image_id": image["image_id"],
                    "commit": commit, "version": version, "prepared_at": int(time.time()),
                    "state": "prepared"}
        write_json(root / "promotion.json", manifest)
        _audit("prepare", run_id=run_id, outcome="passed", details=manifest)
        return manifest
    except Exception as error:
        reason = _failure_code(error)
        write_json(root / "failure.json", {"run_id": run_id, "action": "prepare",
                                            "reason": reason, "error_type": type(error).__name__})
        _audit("prepare", run_id=run_id, outcome="failed",
               details={"reason": reason})
        _alert("prepare", run_id, reason)
        raise

def measure(run_id: str, artifact_id: str) -> dict:
    root, manifest = _read_manifest(run_id)
    if artifact_id != manifest["artifact_id"] or manifest["state"] not in {"prepared", "measured"}:
        refuse("Dev application measurement identity invalid")
    report = measure_development(root / "artifact", root / "image", artifact_id)
    manifest["state"] = "measured"
    _replace_manifest(root, manifest)
    write_json(root / "measure.json", report)
    _audit("measure", run_id=run_id, outcome="passed", details=manifest)
    return report


def reconcile(run_id: str, artifact_id: str) -> dict:
    root, manifest = _read_manifest(run_id)
    if artifact_id != manifest["artifact_id"] or manifest["state"] != "prepared":
        refuse("Dev application reconcile requires prepared candidate")
    evidence = EVIDENCE_ROOT / ("dev-reconcile-" + run_id.removeprefix("dev-update-"))
    report = reconcile_development(root / "artifact", root / "image", artifact_id, evidence)
    write_json(root / "reconcile.json", report)
    _audit("reconcile", run_id=run_id, outcome="passed", details=manifest)
    return report

def apply(run_id: str, artifact_id: str) -> dict:
    root, manifest = _read_manifest(run_id)
    if artifact_id != manifest["artifact_id"] or manifest["state"] != "measured":
        refuse("Dev application apply requires measured candidate")
    evidence = EVIDENCE_ROOT / run_id
    try:
        report = apply_development(root / "artifact", root / "image", artifact_id, evidence)
        manifest["state"] = "applied"
        _replace_manifest(root, manifest)
        _audit("apply", run_id=run_id, outcome="passed", details=manifest)
        return report
    except Exception:
        _audit("apply", run_id=run_id, outcome="failed",
               details={"reason": "dev_application_apply_failed"})
        _alert("apply", run_id, "dev_application_apply_failed")
        raise

def accept(run_id: str, artifact_id: str) -> dict:
    root, manifest = _read_manifest(run_id)
    if artifact_id != manifest["artifact_id"] or manifest["state"] != "applied":
        refuse("Dev application acceptance identity invalid")
    live, _, _ = read_configuration(root_for("development"))
    if (live.get("artifact_id") != artifact_id
            or live.get("images", {}).get("backend") != manifest["image_id"]
            or live.get("commit") != manifest["commit"]
            or live.get("version") != manifest["version"]):
        refuse("live Dev is not the accepted immutable candidate")
    health_report = health("development", manifest["version"])
    result_path = EVIDENCE_ROOT / run_id / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if (result.get("environment") != "development"
            or result.get("artifact_id") != artifact_id
            or result.get("image_id") != manifest["image_id"]
            or result.get("commit") != manifest["commit"]
            or result.get("version") != manifest["version"]
            or result.get("health") is not True):
        refuse("Dev application apply evidence invalid")
    result.update({"environment": "development", "run_id": run_id,
                   "artifact_id": artifact_id, "image_id": manifest["image_id"],
                   "commit": manifest["commit"], "version": manifest["version"],
                   "health": True, "accepted": True, "accepted_at": int(time.time())})
    write_json(EVIDENCE_ROOT / run_id / "accept.json", {**result, **health_report})
    _replace_json(result_path, result)
    manifest["state"] = "accepted"
    _replace_manifest(root, manifest)
    _audit("accept", run_id=run_id, outcome="passed", details=manifest)
    return result

def status() -> dict:
    candidates = BASE / "candidates"
    runs = []
    if candidates.is_dir() and not candidates.is_symlink():
        for path in sorted(candidates.iterdir()):
            if RUN_RE.fullmatch(path.name) and (path / "promotion.json").is_file():
                try:
                    manifest = json.loads((path / "promotion.json").read_text(encoding="utf-8"))
                    runs.append({"run_id": path.name, "state": manifest.get("state")})
                except (OSError, ValueError):
                    runs.append({"run_id": path.name, "state": "invalid"})
    return {"gateway": "dev-application", "environment": "development", "runs": runs[-10:]}


def main() -> None:
    import fcntl
    if os.name != "posix" or os.geteuid() != 0:
        raise SystemExit("root execution required")
    os.umask(0o077)
    _safe_root(BASE, create=True)
    with (BASE / "gateway.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        action = sys.argv[1] if len(sys.argv) > 1 else "invalid"
        run_id = sys.argv[2] if len(sys.argv) > 2 and RUN_RE.fullmatch(sys.argv[2]) else ""
        try:
            if sys.argv[1:] == ["status"]:
                result = status()
            elif len(sys.argv) == 6 and sys.argv[1] == "prepare":
                result = prepare(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
            elif len(sys.argv) == 4 and sys.argv[1] in {"measure", "reconcile", "apply"}:
                result = {"measure": measure, "reconcile": reconcile, "apply": apply}[sys.argv[1]](sys.argv[2], sys.argv[3])
            elif len(sys.argv) == 4 and sys.argv[1] == "accept":
                result = accept(sys.argv[2], sys.argv[3])
            else:
                refuse()
            if action == "status":
                _audit("status", outcome="passed")
            print(json.dumps(result, sort_keys=True))
        except Exception as error:
            if action != "prepare":
                reason = _operation_failure_code(error)
                _audit(action, run_id=run_id, outcome="failed", details={"reason": reason})
                if run_id:
                    root = _run_root(run_id)
                    if root.is_dir() and not root.is_symlink():
                        write_json(root / f"{action}-failure-{time.time_ns()}.json", {
                            "action": action,
                            "reason": reason,
                            "run_id": run_id,
                            "error_type": type(error).__name__,
                        })
                if run_id and action in {"measure", "accept"}:
                    _alert(action, run_id, reason)
            raise SystemExit("Dev application gateway refused; inspect private evidence") from None


if __name__ == "__main__":
    main()
