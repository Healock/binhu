import json
from pathlib import Path

import pytest

from tools import venue_photo_recovery as recovery


@pytest.mark.asyncio
async def test_measurement_keeps_only_accepted_export_subset(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery.settings, "VENUE_PHOTO_DIR", str(tmp_path))
    refs = [
        {
            "submission_id": "accepted-submission",
            "storage_key": "accepted.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 3,
            "sha256": recovery.hashlib.sha256(b"jpg").hexdigest(),
        },
        {
            "submission_id": "rejected-submission",
            "storage_key": "rejected.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 3,
            "sha256": recovery.hashlib.sha256(b"bad").hexdigest(),
        },
    ]
    async def fake_photo_rows(_ids=None):
        return refs

    monkeypatch.setattr(recovery, "_photo_rows", fake_photo_rows)

    item = {
        "submission_id": "accepted-submission",
        "photo_size": 3,
        "photo_ciphertext_sha256": "c" * 64,
        "key_id": "venue-rsa-2026-09-2",
        "photo_mime_type": "image/jpeg",
    }

    class Client:
        async def export_accepted_photo_submissions(self, _ids):
            return [item]

        async def download_recovery_photo(self, *_args):
            return b"encrypted"

    monkeypatch.setattr(recovery, "decrypt_submission", lambda *_args: ({}, b"jpg"))
    monkeypatch.setattr(recovery, "_validate_decrypted_payload", lambda *_args: None)
    result = await recovery._verified_candidates(Client())

    assert [row["submission_id"] for row in result] == ["accepted-submission"]


def test_measurement_is_immutable_and_hash_bound_to_candidate_list(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery.settings, "BACKUP_DIR", tmp_path)
    candidates = [{
        "submission_id": "submission-id",
        "storage_key": "photo.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": 12,
        "sha256": "a" * 64,
        "key_id": "key-1",
        "ciphertext_sha256": "b" * 64,
        "ciphertext_size": 24,
    }]

    artifact = recovery._store_measurement("recovery-20261023-01", candidates)

    assert artifact["measurement_hash"] == recovery._canonical_hash(candidates)
    assert recovery._load_measurement("recovery-20261023-01") == artifact
    with pytest.raises(FileExistsError):
        recovery._store_measurement("recovery-20261023-01", candidates)


def test_measurement_rejects_modified_candidate_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery.settings, "BACKUP_DIR", tmp_path)
    artifact = recovery._store_measurement("recovery-20261023-02", [])
    path = recovery._measurement_path("recovery-20261023-02")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["candidates"].append({"storage_key": "changed.jpg"})
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(RuntimeError, match="measurement_evidence_invalid"):
        recovery._load_measurement("recovery-20261023-02")


def test_apply_requires_verified_registry_and_photo_backup_scope(tmp_path):
    registry_backup = tmp_path / "RegistryData.sql.gz"
    photo_backup = tmp_path / "venue-photos.tar.gz"
    registry_backup.write_bytes(b"registry backup")
    photo_backup.write_bytes(b"photo backup")
    manifest = tmp_path / "backup-manifest.json"
    manifest.write_text(json.dumps({
        "operation": "venue-photo-recovery",
        "run_id": "recovery-20261023-03",
        "database_scope": ["RegistryData"],
        "databases": [
            {"database": "RegistryData", "filename": "RegistryData.sql.gz",
             "size_bytes": registry_backup.stat().st_size,
             "sha256": recovery._sha256_file(registry_backup)},
            {"artifact": "venue-photos.tar.gz", "size_bytes": photo_backup.stat().st_size,
             "sha256": recovery._sha256_file(photo_backup)},
        ],
    }), encoding="utf-8")

    recovery._verify_backup_manifest(manifest, "recovery-20261023-03")

    manifest.write_text(json.dumps({"operation": "venue-photo-recovery", "run_id": "wrong-run"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="backup_manifest_scope_invalid"):
        recovery._verify_backup_manifest(manifest, "recovery-20261023-03")


def test_apply_rejects_backup_artifact_hash_mismatch(tmp_path):
    registry_backup = tmp_path / "RegistryData.sql.gz"
    photo_backup = tmp_path / "venue-photos.tar.gz"
    registry_backup.write_bytes(b"registry backup")
    photo_backup.write_bytes(b"photo backup")
    manifest = tmp_path / "backup-manifest.json"
    manifest.write_text(json.dumps({
        "operation": "venue-photo-recovery",
        "run_id": "recovery-20261023-04",
        "database_scope": ["RegistryData"],
        "databases": [
            {"database": "RegistryData", "filename": "RegistryData.sql.gz",
             "size_bytes": registry_backup.stat().st_size, "sha256": "0" * 64},
            {"artifact": "venue-photos.tar.gz", "size_bytes": photo_backup.stat().st_size,
             "sha256": recovery._sha256_file(photo_backup)},
        ],
    }), encoding="utf-8")

    with pytest.raises(RuntimeError, match="backup_artifact_integrity_invalid"):
        recovery._verify_backup_manifest(manifest, "recovery-20261023-04")
