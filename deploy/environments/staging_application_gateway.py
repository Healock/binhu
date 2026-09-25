"""Fixed Staging application promotion gateway.

The gateway accepts the exact immutable environment artifact already accepted
in Dev, adopts that live Dev backend image without rebuilding it, and only
permits Staging measure/apply/accept operations.  It has no Production apply
target and no caller-controlled filesystem paths.
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
from .image import verify_image
from .runtime import root_for
from .update import health, measure_staging, reconcile_staging, apply_staging, read_configuration


BASE = Path("/var/lib/binhu-staging-application")
AUDIT_ROOT = Path("/var/log/binhu-staging-gateways")
EVIDENCE_ROOT = Path("/srv/deploy-backups/environment-triad")
RUN_RE = re.compile(r"staging-app-([0-9a-f]{16})")
DEV_RUN_RE = re.compile(r"dev-update-[0-9a-f]{16}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
VERSION_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
ARTIFACT_RE = re.compile(r"[0-9a-f]{64}")
IMAGE_RE = re.compile(r"sha256:[0-9a-f]{64}")
ARCHIVE_MEMBERS = {"artifact.json", "source.tar", "frontend.tar"}
CONTROL_COMMIT = Path('/usr/local/libexec/binhu-staging-promotion/control-commit')


def refuse(message: str = "fixed Staging application command required") -> None:
    raise ValueError(message)


def _safe_root(path: Path, *, create: bool = False) -> Path:
    if not path.is_absolute() or path != path.resolve() or any(item.is_symlink() for item in (path, *path.parents)):
        refuse("Staging application path identity invalid")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        refuse("Staging application path permissions invalid")
    return path


def _audit(action: str, *, run_id: str = "", outcome: str, details: dict | None = None) -> None:
    _safe_root(AUDIT_ROOT, create=True)
    allowed: dict[str, object] = {}
    for key, value in (details or {}).items():
        if key in {"artifact_id", "commit", "version", "image_id", "dev_acceptance_run_id", "reason"}:
            if isinstance(value, str) and len(value) <= 128 and re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
                allowed[key] = value
    entry = {"timestamp": int(time.time()), "gateway": "staging-application", "action": action,
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
    write_json(path, {"timestamp": int(time.time()), "gateway": "staging-application",
                      "action": action, "run_id": run_id, "severity": "error", "reason": reason})
    path.chmod(0o600)


def _run_root(run_id: str) -> Path:
    if not RUN_RE.fullmatch(run_id):
        refuse("fixed Staging application run id required")
    root = BASE / "candidates" / run_id
    if root.parent != BASE / "candidates" or root.is_symlink():
        refuse("Staging application candidate path invalid")
    return root


def _read_manifest(run_id: str) -> tuple[Path, dict]:
    root = _run_root(run_id)
    _safe_root(root)
    path = root / "promotion.json"
    if path.is_symlink() or not path.is_file():
        refuse("Staging application promotion manifest missing")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    required = {"schema", "environment", "run_id", "artifact_id", "image_id", "commit", "version",
                "dev_acceptance_run_id", "artifact_manifest_sha256", "prepared_at", "state"}
    if (set(manifest) != required or manifest.get("schema") != 1 or manifest.get("environment") != "staging"
            or manifest.get("run_id") != run_id or not ARTIFACT_RE.fullmatch(str(manifest.get("artifact_id", "")))
            or not IMAGE_RE.fullmatch(str(manifest.get("image_id", "")))
            or not COMMIT_RE.fullmatch(str(manifest.get("commit", "")))
            or not VERSION_RE.fullmatch(str(manifest.get("version", "")))
            or not DEV_RUN_RE.fullmatch(str(manifest.get("dev_acceptance_run_id", "")))
            or manifest.get("state") not in {"prepared", "measured", "applied", "accepted"}):
        refuse("Staging application promotion manifest invalid")
    artifact = root / "artifact"
    image = root / "image"
    actual = verify_artifact(artifact)
    verified = verify_image(artifact, manifest["artifact_id"], image)
    if (actual["artifact_id"] != manifest["artifact_id"] or actual["commit"] != manifest["commit"]
            or actual["version"] != manifest["version"] or verified["image_id"] != manifest["image_id"]
            or hashlib.sha256((artifact / "artifact.json").read_bytes()).hexdigest()
            != manifest["artifact_manifest_sha256"]):
        refuse("Staging application immutable identity changed")
    return root, manifest


def _replace_manifest(root: Path, manifest: dict) -> None:
    target = root / "promotion.json"
    temporary = root / "promotion.json.candidate"
    if temporary.exists() or temporary.is_symlink():
        refuse("Staging application manifest update unresolved")
    write_json(temporary, manifest)
    temporary.chmod(0o600)
    os.replace(temporary, target)


def _extract_artifact(data: bytes, target: Path) -> None:
    if len(data) > 256 * 1024 * 1024:
        refuse("Staging application artifact too large")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        names = {PurePosixPath(item.name).as_posix() for item in members}
        if names != ARCHIVE_MEMBERS or any(not item.isfile() or item.issym() or item.islnk() for item in members):
            refuse("Staging application artifact contents invalid")
        for item in members:
            if item.size < 1 or item.size > 192 * 1024 * 1024:
                refuse("Staging application artifact member invalid")
        archive.extractall(target, filter="data")


def _adopt_dev_image(artifact: Path, candidate: dict, image_id: str, output: Path) -> dict:
    if not IMAGE_RE.fullmatch(image_id) or output.exists() or output.is_symlink():
        refuse("accepted Dev image identity required")
    with tarfile.open(artifact / "source.tar") as archive:
        member = archive.getmember("backend/Dockerfile")
        if not member.isfile():
            refuse("backend Dockerfile missing")
        original = archive.extractfile(member).read()
    appended = ("\nCOPY VERSION /app/VERSION\nENV APP_VERSION=" + candidate["version"] + "\n").encode()
    labels = {"org.opencontainers.image.revision": candidate["commit"],
              "org.opencontainers.image.version": candidate["version"],
              "binhu.artifact.id": candidate["artifact_id"]}
    output.mkdir(mode=0o700)
    write_json(output / "image.json", {"schema": 1, "artifact_id": candidate["artifact_id"],
        "commit": candidate["commit"], "version": candidate["version"], "image_id": image_id,
        "labels": labels, "dockerfile_sha256": hashlib.sha256(original + appended).hexdigest()})
    return verify_image(artifact, candidate["artifact_id"], output)


def prepare(run_id: str, commit: str, version: str, artifact_id: str,
            dev_run_id: str, stream=None) -> dict:
    if (not COMMIT_RE.fullmatch(commit) or not VERSION_RE.fullmatch(version)
            or not ARTIFACT_RE.fullmatch(artifact_id) or not DEV_RUN_RE.fullmatch(dev_run_id)):
        refuse("immutable Dev candidate identity required")
    if (CONTROL_COMMIT.is_symlink() or not CONTROL_COMMIT.is_file()
            or CONTROL_COMMIT.read_text(encoding='ascii').strip() != commit):
        refuse("candidate commit is not the independently installed main revision")
    accepted = _dev_acceptance(dev_run_id, {"artifact_id": artifact_id, "commit": commit,
        "version": version, "image_id": _dev_acceptance_report(dev_run_id)["image_id"]})
    _safe_root(BASE, create=True)
    candidates = _safe_root(BASE / "candidates", create=True)
    root = _run_root(run_id)
    if root.exists() or root.is_symlink():
        refuse("Staging application candidate already exists")
    root.mkdir(mode=0o700)
    artifact = root / "artifact"
    artifact.mkdir(mode=0o700)
    try:
        data = (stream or sys.stdin.buffer).read(256 * 1024 * 1024 + 1)
        _extract_artifact(data, artifact)
        candidate = verify_artifact(artifact)
        if (candidate["commit"] != commit or candidate["version"] != version
                or candidate["artifact_id"] != artifact_id):
            refuse("Staging application artifact is not the accepted Dev artifact")
        image = _adopt_dev_image(artifact, candidate, accepted["image_id"], root / "image")
        manifest = {"schema": 1, "environment": "staging", "run_id": run_id,
                    "artifact_id": candidate["artifact_id"], "image_id": image["image_id"],
                    "commit": commit, "version": version, "dev_acceptance_run_id": dev_run_id,
                    "artifact_manifest_sha256": hashlib.sha256((artifact / "artifact.json").read_bytes()).hexdigest(),
                    "prepared_at": int(time.time()), "state": "prepared"}
        write_json(root / "promotion.json", manifest)
        (root / "promotion.json").chmod(0o600)
        _audit("prepare", run_id=run_id, outcome="passed", details=manifest)
        return {key: manifest[key] for key in ("environment", "run_id", "artifact_id", "image_id",
                                                "commit", "version", "dev_acceptance_run_id", "state")}
    except Exception:
        write_json(root / "failure.json", {"run_id": run_id, "action": "prepare", "reason": "candidate_prepare_failed"})
        (root / "failure.json").chmod(0o600)
        _audit("prepare", run_id=run_id, outcome="failed", details={"reason": "candidate_prepare_failed"})
        _alert("prepare", run_id, "candidate_prepare_failed")
        raise


def measure(run_id: str, artifact_id: str) -> dict:
    root, manifest = _read_manifest(run_id)
    if artifact_id != manifest["artifact_id"] or manifest["state"] not in {"prepared", "measured"}:
        refuse("Staging application measurement identity invalid")
    _dev_acceptance(manifest["dev_acceptance_run_id"], manifest)
    report = measure_staging(root / "artifact", root / "image", artifact_id)
    manifest["state"] = "measured"
    _replace_manifest(root, manifest)
    write_json(root / "measure.json", report)
    _audit("measure", run_id=run_id, outcome="passed", details=manifest)
    return report


def reconcile(run_id: str, artifact_id: str) -> dict:
    root, manifest = _read_manifest(run_id)
    if artifact_id != manifest["artifact_id"] or manifest["state"] not in {"prepared", "measured"}:
        refuse("Staging application reconciliation identity invalid")
    _dev_acceptance(manifest["dev_acceptance_run_id"], manifest)
    evidence = EVIDENCE_ROOT / ("staging-reconcile-" + RUN_RE.fullmatch(run_id).group(1))
    report = reconcile_staging(root / "artifact", root / "image", artifact_id, evidence)
    return report


def apply(run_id: str, artifact_id: str) -> dict:
    root, manifest = _read_manifest(run_id)
    if artifact_id != manifest["artifact_id"] or manifest["state"] != "measured":
        refuse("Staging application apply requires measured candidate")
    _dev_acceptance(manifest["dev_acceptance_run_id"], manifest)
    evidence = EVIDENCE_ROOT / ("staging-update-" + RUN_RE.fullmatch(run_id).group(1))
    try:
        report = apply_staging(root / "artifact", root / "image", artifact_id, evidence)
        manifest["state"] = "applied"
        _replace_manifest(root, manifest)
        _audit("apply", run_id=run_id, outcome="passed", details=manifest)
        return report
    except Exception:
        _audit("apply", run_id=run_id, outcome="failed", details={"reason": "staging_apply_failed"})
        _alert("apply", run_id, "staging_apply_failed")
        raise


def _dev_acceptance_report(dev_run_id: str) -> dict:
    if not DEV_RUN_RE.fullmatch(dev_run_id):
        refuse("fixed Dev acceptance run id required")
    path = EVIDENCE_ROOT / dev_run_id / "result.json"
    if path.is_symlink() or not path.is_file():
        refuse("Dev acceptance evidence missing")
    report = json.loads(path.read_text(encoding="utf-8"))
    if (report.get("environment") != "development" or report.get("health") is not True
            or not ARTIFACT_RE.fullmatch(str(report.get("artifact_id", "")))
            or not IMAGE_RE.fullmatch(str(report.get("image_id", "")))
            or not COMMIT_RE.fullmatch(str(report.get("commit", "")))
            or not VERSION_RE.fullmatch(str(report.get("version", "")))):
        refuse("Dev acceptance evidence invalid")
    return report


def _dev_acceptance(dev_run_id: str, manifest: dict) -> dict:
    report = _dev_acceptance_report(dev_run_id)
    if (report.get("artifact_id") != manifest["artifact_id"]
            or report.get("image_id") != manifest["image_id"]
            or report.get("commit") != manifest["commit"]
            or report.get("version") != manifest["version"]):
        refuse("Dev acceptance is not the same immutable candidate")
    dev_manifest, _, _ = read_configuration(root_for("development"))
    if (dev_manifest.get("artifact_id") != manifest["artifact_id"]
            or dev_manifest.get("images", {}).get("backend") != manifest["image_id"]
            or dev_manifest.get("commit") != manifest["commit"]
            or dev_manifest.get("version") != manifest["version"]):
        refuse("live Dev is not the accepted immutable candidate")
    return report


def accept(run_id: str, artifact_id: str, dev_run_id: str) -> dict:
    root, manifest = _read_manifest(run_id)
    if artifact_id != manifest["artifact_id"] or manifest["state"] != "applied":
        refuse("Staging application acceptance identity invalid")
    if dev_run_id != manifest["dev_acceptance_run_id"]:
        refuse("Staging application Dev acceptance binding changed")
    _dev_acceptance(dev_run_id, manifest)
    staging_manifest, _, _ = read_configuration(root_for("staging"))
    if (staging_manifest.get("artifact_id") != artifact_id
            or staging_manifest.get("images", {}).get("backend") != manifest["image_id"]):
        refuse("live Staging is not the immutable candidate")
    health("staging", manifest["version"])
    manifest["state"] = "accepted"
    _replace_manifest(root, manifest)
    report = {"environment": "staging", "run_id": run_id, "artifact_id": artifact_id,
              "image_id": manifest["image_id"], "commit": manifest["commit"],
              "version": manifest["version"], "dev_acceptance_run_id": dev_run_id,
              "accepted": True, "production_modified": False}
    write_json(root / "accept.json", report)
    _audit("accept", run_id=run_id, outcome="passed", details=report)
    return report


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
    return {"gateway": "staging-application", "environment": "staging", "runs": runs[-10:]}


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
            elif len(sys.argv) == 7 and sys.argv[1] == "prepare":
                result = prepare(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])
            elif len(sys.argv) == 4 and sys.argv[1] in {"measure", "reconcile", "apply"}:
                result = {"measure": measure, "reconcile": reconcile, "apply": apply}[sys.argv[1]](sys.argv[2], sys.argv[3])
            elif len(sys.argv) == 5 and sys.argv[1] == "accept":
                result = accept(sys.argv[2], sys.argv[3], sys.argv[4])
            else:
                refuse()
            if action == "status":
                _audit("status", outcome="passed")
            print(json.dumps(result, sort_keys=True))
        except Exception:
            if action not in {"prepare", "apply"}:
                _audit(action, run_id=run_id, outcome="failed", details={"reason": "gateway_operation_failed"})
                if run_id and action in {"measure", "accept"}:
                    _alert(action, run_id, "gateway_operation_failed")
            raise SystemExit("Staging application gateway refused; inspect private evidence") from None


if __name__ == "__main__":
    main()
