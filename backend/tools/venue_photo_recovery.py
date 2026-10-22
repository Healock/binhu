"""Restore missing Production venue photos from accepted encrypted submissions."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import uuid
from pathlib import Path
from typing import Any

import aiomysql

from config import settings
from services.venue_cloud import _validate_decrypted_payload
from services.venue_cloud_client import VenueCloudClient
from services.venue_cloud_security import decrypt_submission


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")
CONFIRMATION = "confirm-venue-photo-recovery"
MAX_BATCH = 50


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _connect_registry():
    return await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_REGISTRY_DB,
        connect_timeout=8,
        autocommit=True,
        charset="utf8mb4",
    )


async def _photo_rows(submission_ids: list[str] | None = None) -> list[dict[str, Any]]:
    conn = await _connect_registry()
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            sql = (
                "SELECT visit.cloud_submission_id,photo.storage_key,photo.mime_type,photo.size_bytes,photo.sha256 "
                "FROM _venue_visits visit JOIN _venue_visit_photos photo ON photo.visit_id=visit.id "
                "WHERE visit.source='cloud_public_qr' AND visit.cloud_submission_id IS NOT NULL "
                "AND visit.deleted_at IS NULL AND visit.retention_until>UTC_TIMESTAMP() "
                "AND photo.deleted_at IS NULL AND photo.retention_until>UTC_TIMESTAMP() "
            )
            params: tuple[str, ...] = ()
            if submission_ids is not None:
                if not submission_ids:
                    return []
                sql += "AND visit.cloud_submission_id IN (" + ",".join(["%s"] * len(submission_ids)) + ") "
                params = tuple(submission_ids)
            await cur.execute(sql + "ORDER BY visit.cloud_submission_id", params)
            rows = list(await cur.fetchall())
    finally:
        conn.close()
    return [{
        "submission_id": str(row["cloud_submission_id"]),
        "storage_key": str(row["storage_key"]),
        "mime_type": str(row["mime_type"]),
        "size_bytes": int(row["size_bytes"]),
        "sha256": str(row["sha256"]),
    } for row in rows]


def _measurement_path(run_id: str) -> Path:
    return Path(settings.BACKUP_DIR).resolve() / ".venue-photo-recovery" / f"{run_id}.json"


def _store_measurement(run_id: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    path = _measurement_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    artifact = {"schema": 1, "run_id": run_id, "candidates": candidates, "measurement_hash": _canonical_hash(candidates)}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(artifact, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    return artifact


def _load_measurement(run_id: str) -> dict[str, Any]:
    path = _measurement_path(run_id)
    if not path.is_file():
        raise RuntimeError("measurement_evidence_missing")
    artifact = json.loads(path.read_text(encoding="utf-8"))
    candidates = artifact.get("candidates")
    if (
        artifact.get("schema") != 1
        or artifact.get("run_id") != run_id
        or not isinstance(candidates, list)
        or artifact.get("measurement_hash") != _canonical_hash(candidates)
    ):
        raise RuntimeError("measurement_evidence_invalid")
    return artifact


async def _verified_candidates(
    client: VenueCloudClient,
    targets: list[dict[str, Any]] | None = None,
    *,
    allow_valid_existing: bool = False,
) -> list[dict[str, Any]]:
    refs = await _photo_rows([row["submission_id"] for row in targets]) if targets is not None else await _photo_rows()
    root = Path(settings.VENUE_PHOTO_DIR).resolve()
    if targets is not None:
        by_id = {row["submission_id"]: row for row in refs}
        refs = []
        for measured in targets:
            actual = by_id.get(str(measured["submission_id"]))
            if not actual or any(str(actual[key]) != str(measured[key]) for key in ("storage_key", "mime_type", "size_bytes", "sha256")):
                raise RuntimeError("production_photo_reference_changed")
            refs.append(actual)
    else:
        refs = [row for row in refs if not (root / row["storage_key"]).is_file()]
    missing = []
    for row in refs:
        target = (root / row["storage_key"]).resolve()
        if root not in target.parents:
            raise RuntimeError("invalid_photo_storage_key")
        if target.exists():
            if not allow_valid_existing or not target.is_file() or target.stat().st_size != row["size_bytes"]:
                raise RuntimeError("target_photo_changed_during_measure")
            if _sha256_file(target) != row["sha256"]:
                raise RuntimeError("target_photo_hash_conflict")
        missing.append(row)
    if not missing:
        return []
    candidates: list[dict[str, Any]] = []
    for offset in range(0, len(missing), MAX_BATCH):
        batch = missing[offset : offset + MAX_BATCH]
        ids = [row["submission_id"] for row in batch]
        exported = await client.export_accepted_photo_submissions(ids)
        by_id = {str(item["submission_id"]): item for item in exported}
        if len(by_id) != len(batch) or set(by_id) != set(ids):
            raise RuntimeError("accepted_submission_mapping_incomplete")
        for target in batch:
            item = by_id[target["submission_id"]]
            encrypted_photo = await client.download_recovery_photo(
                target["submission_id"], str(uuid.uuid4()), int(item["photo_size"]),
                str(item["photo_ciphertext_sha256"]),
            )
            payload, photo = decrypt_submission(item, encrypted_photo, settings.VENUE_CLOUD_DECRYPTION_KEY_DIR)
            _validate_decrypted_payload(payload, photo, str(item["photo_mime_type"]))
            if (
                str(item["key_id"]) == ""
                or str(item["photo_mime_type"]) != target["mime_type"]
                or len(photo) != target["size_bytes"]
                or hashlib.sha256(photo).hexdigest() != target["sha256"]
            ):
                raise RuntimeError("decrypted_photo_does_not_match_registry")
            path = (root / target["storage_key"]).resolve()
            if path.exists() and not allow_valid_existing:
                raise RuntimeError("target_photo_changed_during_measure")
            candidates.append({
                **target,
                "key_id": str(item["key_id"]),
                "ciphertext_sha256": str(item["photo_ciphertext_sha256"]),
                "ciphertext_size": int(item["photo_size"]),
            })
    return candidates


def _summary(command: str, run_id: str, candidates: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "command": command,
        "ok": True,
        "run_id": run_id,
        "candidate_count": len(candidates),
        "measurement_hash": _canonical_hash(candidates),
        "key_ids": dict(sorted({key: sum(1 for item in candidates if item["key_id"] == key) for key in {item["key_id"] for item in candidates}}.items())),
        **extra,
    }


def _verify_backup_manifest(path: Path, run_id: str) -> None:
    if not path.is_file():
        raise RuntimeError("backup_manifest_missing")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    artifacts = manifest.get("databases", [])
    required = {
        "RegistryData.sql.gz": next((item for item in artifacts if item.get("database") == "RegistryData"), None),
        "venue-photos.tar.gz": next((item for item in artifacts if item.get("artifact") == "venue-photos.tar.gz"), None),
    }
    valid = (
        manifest.get("operation") == "venue-photo-recovery"
        and manifest.get("run_id") == run_id
        and "RegistryData" in manifest.get("database_scope", [])
        and all(
            entry is not None
            and entry.get("filename", filename) == filename
            and type(entry.get("size_bytes")) is int
            and entry["size_bytes"] >= 0
            and re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))
            for filename, entry in required.items()
        )
    )
    if not valid:
        raise RuntimeError("backup_manifest_scope_invalid")
    for filename, entry in required.items():
        artifact_path = path.parent / filename
        if (
            not artifact_path.is_file()
            or artifact_path.stat().st_size != entry["size_bytes"]
            or _sha256_file(artifact_path) != entry["sha256"]
        ):
            raise RuntimeError("backup_artifact_integrity_invalid")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if not RUN_ID_RE.fullmatch(args.run_id):
        raise ValueError("invalid_run_id")
    client = VenueCloudClient()
    try:
        if args.phase == "measure":
            candidates = await _verified_candidates(client)
            artifact = _store_measurement(args.run_id, candidates)
            summary = _summary(args.phase, args.run_id, candidates)
            summary["measurement_hash"] = artifact["measurement_hash"]
            return summary
        if args.phase == "apply":
            artifact = _load_measurement(args.run_id)
            candidates = await _verified_candidates(client, artifact["candidates"], allow_valid_existing=True)
            summary = _summary(args.phase, args.run_id, candidates)
            if (
                args.confirm != CONFIRMATION
                or args.expected_count != len(candidates)
                or args.measurement_hash != artifact["measurement_hash"]
                or args.measurement_hash != summary["measurement_hash"]
            ):
                raise RuntimeError("apply_confirmation_or_count_mismatch")
            _verify_backup_manifest(args.backup_manifest, args.run_id)
            root = Path(settings.VENUE_PHOTO_DIR).resolve()
            restored = 0
            for candidate in candidates:
                target = (root / candidate["storage_key"]).resolve()
                if target.is_file() and target.stat().st_size == candidate["size_bytes"] and _sha256_file(target) == candidate["sha256"]:
                    continue
                item = (await client.export_accepted_photo_submissions([candidate["submission_id"]]))[0]
                if (
                    item["key_id"] != candidate["key_id"]
                    or item["photo_ciphertext_sha256"] != candidate["ciphertext_sha256"]
                    or int(item["photo_size"]) != candidate["ciphertext_size"]
                ):
                    raise RuntimeError("source_photo_changed_after_measure")
                encrypted_photo = await client.download_recovery_photo(
                    candidate["submission_id"], str(uuid.uuid4()), int(item["photo_size"]),
                    str(item["photo_ciphertext_sha256"]),
                )
                payload, photo = decrypt_submission(item, encrypted_photo, settings.VENUE_CLOUD_DECRYPTION_KEY_DIR)
                _validate_decrypted_payload(payload, photo, str(item["photo_mime_type"]))
                if hashlib.sha256(photo).hexdigest() != candidate["sha256"] or len(photo) != candidate["size_bytes"]:
                    raise RuntimeError("photo_changed_after_measure")
                if root not in target.parents:
                    raise RuntimeError("invalid_photo_storage_key")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.restore")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(temporary, flags, 0o640)
                try:
                    os.fchmod(descriptor, 0o640)
                    with os.fdopen(descriptor, "wb", closefd=False) as output:
                        output.write(photo)
                        output.flush()
                        os.fsync(descriptor)
                    os.link(temporary, target, follow_symlinks=False)
                finally:
                    os.close(descriptor)
                    temporary.unlink(missing_ok=True)
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                restored += 1
            return {**summary, "restored_count": restored}
        artifact = _load_measurement(args.run_id)
        candidates = artifact["candidates"]
        refs = await _photo_rows([row["submission_id"] for row in candidates])
        by_id = {row["submission_id"]: row for row in refs}
        root = Path(settings.VENUE_PHOTO_DIR).resolve()
        present = 0
        mismatched = 0
        missing = 0
        for candidate in candidates:
            row = by_id.get(candidate["submission_id"])
            if not row or any(str(row[key]) != str(candidate[key]) for key in ("storage_key", "mime_type", "size_bytes", "sha256")):
                mismatched += 1
                continue
            path = (root / candidate["storage_key"]).resolve()
            if root not in path.parents or not path.is_file():
                missing += 1
                continue
            digest = _sha256_file(path)
            if path.stat().st_size != candidate["size_bytes"] or digest != candidate["sha256"]:
                mismatched += 1
            else:
                present += 1
        return {
            "command": "verify", "ok": missing == 0 and mismatched == 0 and present == len(candidates), "run_id": args.run_id,
            "candidate_count": len(candidates), "present_and_valid": present,
            "missing": missing, "mismatched": mismatched,
        }
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Controlled venue photo recovery")
    subparsers = parser.add_subparsers(dest="phase", required=True)
    for phase in ("measure", "verify"):
        command = subparsers.add_parser(phase)
        command.add_argument("--run-id", required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--run-id", required=True)
    apply.add_argument("--expected-count", required=True, type=int)
    apply.add_argument("--measurement-hash", required=True)
    apply.add_argument("--backup-manifest", required=True, type=Path)
    apply.add_argument("--confirm", required=True)
    return parser.parse_args()


def main() -> None:
    try:
        print(json.dumps(asyncio.run(run(parse_args())), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except Exception as exc:
        print(json.dumps({"command": "venue_photo_recovery", "ok": False, "error_code": type(exc).__name__}, separators=(",", ":")))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
