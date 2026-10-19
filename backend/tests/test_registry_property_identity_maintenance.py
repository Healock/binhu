from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from migrations import registry_property_identity_maintenance as maintenance


RUN_ID = "identity-run-2026-10-19"
ACTOR = "codex-maintainer"
OLD_ID = 701
CANONICAL_ID = 702
MEASURE_HASH = "a" * 64
POST_HASH = "b" * 64
CONTENT_HASH = "c" * 64


class Cursor:
    def __init__(self, *, mapping=None, certificates=(), sources=(), certificate_row=None, certificate_count=1):
        self.sql = ""
        self.params = ()
        self.calls = []
        self.rowcount = 0
        self.mapping = mapping
        self.certificates = list(certificates)
        self.sources = list(sources)
        self.certificate_row = certificate_row or (CANONICAL_ID, "source-hash")
        self.certificate_count = certificate_count

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, sql, params=()):
        self.sql, self.params = sql, params
        self.calls.append((sql, params))
        self.rowcount = 1 if sql.lstrip().upper().startswith(("INSERT", "UPDATE")) else 0

    async def fetchone(self):
        if "FROM registry_property_identity_mappings" in self.sql and "WHERE old_property_id=%s" in self.sql:
            return self.mapping
        if "SELECT property_id,source_content_hash FROM registry_property_certificates WHERE id=%s" in self.sql:
            return self.certificate_row
        if "SELECT COUNT(*) FROM registry_property_certificates" in self.sql:
            return (self.certificate_count,)
        return None

    async def fetchall(self):
        if "SELECT id,source_content_hash FROM registry_property_certificates" in self.sql:
            return self.certificates
        if "FROM registry_source_records" in self.sql and "entity_type='property_certificate'" in self.sql:
            return self.sources
        return []


class Connection:
    def __init__(self, cursor: Cursor):
        self.fake_cursor = cursor
        self.begin = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.close = AsyncMock()

    def cursor(self):
        return self.fake_cursor


def measurement(measurement_hash: str, *, disposition="no_references_candidate", summary=None):
    return {
        "measurement_hash": measurement_hash,
        "disposition": disposition,
        "properties": [
            {"id": OLD_ID, "community_id": 8},
            {"id": CANONICAL_ID, "community_id": 8},
        ],
        "reference_summary": summary or {"blocking_reference_count": 0},
    }


def mapping_row(*, status="active", version=1, original_hash=MEASURE_HASH,
                post_apply_hash=POST_HASH, post_rollback_hash="", candidate_class="no_references_candidate"):
    return (
        CANONICAL_ID, 8, status, RUN_ID, version, original_hash,
        post_apply_hash, post_rollback_hash, candidate_class, ACTOR,
    )


@pytest.mark.parametrize("old_id,canonical_id", [(0, 2), (-1, 2), (1, 1), (2**63, 3)])
def test_invalid_pair_is_rejected_before_database_access(old_id, canonical_id):
    with pytest.raises(maintenance.IdentityMaintenanceError, match="invalid_property_pair"):
        maintenance.validate_args(
            run_id=RUN_ID, old_property_id=old_id,
            canonical_property_id=canonical_id, actor=ACTOR,
        )


def test_invalid_maintenance_actor_is_rejected():
    with pytest.raises(maintenance.IdentityMaintenanceError, match="invalid_maintenance_actor"):
        maintenance.validate_args(
            run_id=RUN_ID, old_property_id=OLD_ID,
            canonical_property_id=CANONICAL_ID, actor="bad actor",
        )


@pytest.mark.asyncio
async def test_database_query_and_lock_wait_budgets_are_fixed_and_bounded():
    cursor = Cursor()
    await maintenance._set_query_budget(cursor)
    assert cursor.calls == [
        ("SET SESSION MAX_EXECUTION_TIME=%s", (30000,)),
        ("SET SESSION lock_wait_timeout=%s", (2,)),
        ("SET SESSION innodb_lock_wait_timeout=%s", (5,)),
    ]


def test_parser_exposes_only_fixed_single_pair_operations():
    parsed = maintenance.parser().parse_args([
        "migrate", "--apply", "--environment", "production", "--run-id", RUN_ID,
        "--old-property-id", str(OLD_ID), "--canonical-property-id", str(CANONICAL_ID),
        "--actor", ACTOR,
        "--measurement-hash", MEASURE_HASH, "--backup-manifest", "/root/binhu/deploy-backups/automated/x/backup-manifest.json",
        "--confirm", maintenance.CONFIRM_APPLY,
    ])
    assert parsed.command == "migrate" and parsed.apply is True
    with pytest.raises(SystemExit):
        maintenance.parser().parse_args(["migrate", "--apply", "--sql", "UPDATE anything"])


def test_backup_manifest_is_bound_to_run_database_and_sha256(tmp_path):
    backup_root = tmp_path / "automated"
    backup_root.mkdir()
    artifact = backup_root / "RegistryData.sql.gz"
    artifact.write_bytes(b"synthetic compressed backup bytes")
    manifest = backup_root / "backup-manifest.json"
    manifest.write_text(json.dumps({
        "operation": "registry-property-identity",
        "run_id": RUN_ID,
        "databases": [{
            "database": "RegistryData",
            "filename": artifact.name,
            "size_bytes": artifact.stat().st_size,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }],
    }), encoding="utf-8")
    with patch.object(maintenance, "BACKUP_ROOT", backup_root):
        assert maintenance._verify_manifest(str(manifest), run_id=RUN_ID)["run_id"] == RUN_ID
        artifact.write_bytes(b"changed backup")
        with pytest.raises(maintenance.IdentityMaintenanceError, match="backup_checksum_mismatch"):
            maintenance._verify_manifest(str(manifest), run_id=RUN_ID)


@pytest.mark.asyncio
async def test_reference_summary_allows_only_exact_certificate_exception():
    cursor = Cursor()
    old_certificate = {
        "property_id": OLD_ID, "schema": "RegistryData", "table": "registry_property_certificates",
        "column": "property_id", "kind": "direct", "count": 1,
    }
    old_source = {
        "property_id": OLD_ID, "schema": "RegistryData", "table": "registry_source_records",
        "column": "entity_id", "kind": "certificate_source_reference", "count": 1,
    }
    with (
        patch.object(maintenance, "database_scope", return_value=["RegistryData", "OnlineData"]),
        patch.object(maintenance, "discover_reference_inventory", AsyncMock(return_value=([], [], []))),
        patch.object(maintenance, "scan_references", AsyncMock(return_value=[old_certificate])),
        patch.object(maintenance, "scan_certificate_source_references", AsyncMock(return_value=[old_source])),
        patch.object(maintenance, "scan_opaque_json_reference_markers", AsyncMock(return_value=[])),
    ):
        result = await maintenance._reference_summary(cursor, OLD_ID, CANONICAL_ID)
    assert result["disposition"] == "certificate_only_exception_candidate"
    assert result["old_certificate_count"] == 1
    assert result["canonical_certificate_count"] == 0
    assert "address" not in json.dumps(result).lower()


@pytest.mark.asyncio
async def test_reference_summary_blocks_direct_and_unresolved_json_references():
    cursor = Cursor()
    direct = {
        "property_id": OLD_ID, "schema": "OnlineData", "table": "_task_registration_links",
        "column": "property_id", "kind": "direct", "count": 1,
    }
    with (
        patch.object(maintenance, "database_scope", return_value=["RegistryData", "OnlineData"]),
        patch.object(maintenance, "discover_reference_inventory", AsyncMock(return_value=([], [], []))),
        patch.object(maintenance, "scan_references", AsyncMock(return_value=[direct])),
        patch.object(maintenance, "scan_certificate_source_references", AsyncMock(return_value=[])),
        patch.object(maintenance, "scan_opaque_json_reference_markers", AsyncMock(return_value=[])),
    ):
        assert (await maintenance._reference_summary(cursor, OLD_ID, CANONICAL_ID))["disposition"] == "blocked"
    with (
        patch.object(maintenance, "database_scope", return_value=["RegistryData", "OnlineData"]),
        patch.object(maintenance, "discover_reference_inventory", AsyncMock(return_value=([], [{"schema": "RegistryData", "table": "opaque", "column": "payload"}], []))),
        patch.object(maintenance, "scan_references", AsyncMock(return_value=[])),
        patch.object(maintenance, "scan_certificate_source_references", AsyncMock(return_value=[])),
        patch.object(maintenance, "scan_opaque_json_reference_markers", AsyncMock(return_value=[{
            "property_id": OLD_ID, "schema": "RegistryData", "table": "opaque", "column": "payload", "count": 1,
        }])),
    ):
        result = await maintenance._reference_summary(cursor, OLD_ID, CANONICAL_ID)
    assert result["disposition"] == "blocked"
    assert result["opaque_json_old_reference_count"] == 1


@pytest.mark.asyncio
async def test_measurement_hash_covers_natural_address_without_emitting_it():
    def property_row(property_id, natural_address):
        return (
            property_id, 8, "", natural_address, "", "", "规范地址摘要输入", "house-number",
            "2026-01-01", "出租房", "住宅", "已注销", "household", "active", 1,
        )

    rows = [property_row(OLD_ID, "合成道路一号"), property_row(CANONICAL_ID, "合成道路一号")]
    changed = [rows[0], property_row(CANONICAL_ID, "合成道路二号")]
    references = {
        "disposition": "no_references_candidate", "blocking_reference_count": 0,
        "references": [], "opaque_json_scan": [],
    }
    with (
        patch.object(maintenance, "database_scope", return_value=["RegistryData"]),
        patch.object(maintenance.settings, "APP_ENVIRONMENT", "production"),
        patch.object(maintenance, "_candidate_rows", AsyncMock(side_effect=[rows, changed])),
        patch.object(maintenance, "_reference_summary", AsyncMock(return_value=references)),
    ):
        first = await maintenance._measure_cursor(
            object(), run_id=RUN_ID, actor=ACTOR, old_id=OLD_ID, canonical_id=CANONICAL_ID,
        )
        second = await maintenance._measure_cursor(
            object(), run_id=RUN_ID, actor=ACTOR, old_id=OLD_ID, canonical_id=CANONICAL_ID,
        )
    assert first["measurement_hash"] != second["measurement_hash"]
    assert "合成道路一号" not in json.dumps(first, ensure_ascii=False)


@pytest.mark.asyncio
async def test_apply_migrates_only_certificate_property_link_in_one_transaction():
    cursor = Cursor(certificates=[(91, "source-hash")], sources=[(301,)])
    conn = Connection(cursor)
    baseline = measurement(MEASURE_HASH, disposition="certificate_only_exception_candidate")
    post = measurement(POST_HASH)
    with (
        patch.object(maintenance, "validate_database_scope"),
        patch.object(maintenance, "_verify_manifest", return_value={"run_id": RUN_ID}),
        patch.object(maintenance, "open_connection", AsyncMock(return_value=conn)),
        patch.object(maintenance, "close_connection", AsyncMock()),
        patch.object(maintenance, "_check_scope", AsyncMock()),
        patch.object(maintenance, "_require_maintenance_schema", AsyncMock()),
        patch.object(maintenance, "_lock", AsyncMock()),
        patch.object(maintenance, "_unlock", AsyncMock()),
        patch.object(maintenance, "_measure_cursor", AsyncMock(side_effect=[baseline, post])),
    ):
        result = await maintenance.run_apply(
            run_id=RUN_ID, old_property_id=OLD_ID, canonical_property_id=CANONICAL_ID,
            measurement_hash=MEASURE_HASH, backup_manifest="manifest", confirmation=maintenance.CONFIRM_APPLY,
            actor=ACTOR,
        )
    assert result["certificate_references_moved"] == 1
    assert result["post_apply_measurement_hash"] == POST_HASH
    assert any(sql.startswith("UPDATE registry_property_certificates SET property_id") for sql, _ in cursor.calls)
    assert any("INSERT INTO registry_property_identity_reference_events" in sql for sql, _ in cursor.calls)
    assert conn.commit.await_count == 1
    conn.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_rolls_back_when_certificate_source_reference_is_not_unique():
    cursor = Cursor(certificates=[(91, "source-hash")], sources=[])
    conn = Connection(cursor)
    with (
        patch.object(maintenance, "validate_database_scope"),
        patch.object(maintenance, "_verify_manifest", return_value={"run_id": RUN_ID}),
        patch.object(maintenance, "open_connection", AsyncMock(return_value=conn)),
        patch.object(maintenance, "close_connection", AsyncMock()),
        patch.object(maintenance, "_check_scope", AsyncMock()),
        patch.object(maintenance, "_require_maintenance_schema", AsyncMock()),
        patch.object(maintenance, "_lock", AsyncMock()),
        patch.object(maintenance, "_unlock", AsyncMock()),
        patch.object(maintenance, "_measure_cursor", AsyncMock(return_value=measurement(
            MEASURE_HASH, disposition="certificate_only_exception_candidate",
        ))),
    ):
        with pytest.raises(maintenance.IdentityMaintenanceError, match="certificate_source_reference_changed"):
            await maintenance.run_apply(
                run_id=RUN_ID, old_property_id=OLD_ID, canonical_property_id=CANONICAL_ID,
                measurement_hash=MEASURE_HASH, backup_manifest="manifest", confirmation=maintenance.CONFIRM_APPLY,
                actor=ACTOR,
            )
    conn.commit.assert_not_awaited()
    conn.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_apply_explicitly_rolls_back_open_transaction():
    conn = Connection(Cursor())
    with (
        patch.object(maintenance, "validate_database_scope"),
        patch.object(maintenance, "_verify_manifest", return_value={"run_id": RUN_ID}),
        patch.object(maintenance, "open_connection", AsyncMock(return_value=conn)),
        patch.object(maintenance, "close_connection", AsyncMock()),
        patch.object(maintenance, "_check_scope", AsyncMock()),
        patch.object(maintenance, "_require_maintenance_schema", AsyncMock()),
        patch.object(maintenance, "_lock", AsyncMock()),
        patch.object(maintenance, "_unlock", AsyncMock()),
        patch.object(maintenance, "_measure_cursor", AsyncMock(side_effect=asyncio.CancelledError())),
    ):
        with pytest.raises(asyncio.CancelledError):
            await maintenance.run_apply(
                run_id=RUN_ID, old_property_id=OLD_ID, canonical_property_id=CANONICAL_ID,
                measurement_hash=MEASURE_HASH, backup_manifest="manifest", confirmation=maintenance.CONFIRM_APPLY,
                actor=ACTOR,
            )
    conn.commit.assert_not_awaited()
    conn.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeat_apply_is_idempotent_only_when_post_apply_hash_matches():
    cursor = Cursor(mapping=(OLD_ID, CANONICAL_ID, 8, "active", RUN_ID, 1, MEASURE_HASH, POST_HASH, "no_references_candidate", ACTOR))
    conn = Connection(cursor)
    with (
        patch.object(maintenance, "validate_database_scope"),
        patch.object(maintenance, "_verify_manifest", return_value={"run_id": RUN_ID}),
        patch.object(maintenance, "open_connection", AsyncMock(return_value=conn)),
        patch.object(maintenance, "close_connection", AsyncMock()),
        patch.object(maintenance, "_check_scope", AsyncMock()),
        patch.object(maintenance, "_require_maintenance_schema", AsyncMock()),
        patch.object(maintenance, "_lock", AsyncMock()),
        patch.object(maintenance, "_unlock", AsyncMock()),
        patch.object(maintenance, "_measure_cursor", AsyncMock(return_value=measurement(POST_HASH))),
        patch.object(maintenance, "_mapping_events_match", AsyncMock(return_value=True)),
        patch.object(maintenance, "_certificate_audit_matches", AsyncMock(return_value=True)),
    ):
        result = await maintenance.run_apply(
            run_id=RUN_ID, old_property_id=OLD_ID, canonical_property_id=CANONICAL_ID,
            measurement_hash=MEASURE_HASH, backup_manifest="manifest", confirmation=maintenance.CONFIRM_APPLY,
            actor=ACTOR,
        )
    assert result["idempotent"] is True
    conn.commit.assert_not_awaited()
    conn.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_checks_mapping_hash_reference_audit_and_rollback_state():
    cursor = Cursor(mapping=mapping_row())
    conn = Connection(cursor)
    summary = {
        "blocking_reference_count": 0, "old_certificate_count": 0,
        "old_certificate_source_count": 0, "old_small_community_link_count": 0,
        "canonical_certificate_count": 0, "canonical_certificate_source_count": 0,
    }
    with (
        patch.object(maintenance, "validate_database_scope"),
        patch.object(maintenance, "open_connection", AsyncMock(return_value=conn)),
        patch.object(maintenance, "close_connection", AsyncMock()),
        patch.object(maintenance, "_check_scope", AsyncMock()),
        patch.object(maintenance, "_require_maintenance_schema", AsyncMock()),
        patch.object(maintenance, "_measure_cursor", AsyncMock(return_value=measurement(POST_HASH, summary=summary))),
        patch.object(maintenance, "_mapping_events_match", AsyncMock(return_value=True)),
        patch.object(maintenance, "_certificate_audit_matches", AsyncMock(return_value=True)),
    ):
        result = await maintenance.run_verify(
            run_id=RUN_ID, old_property_id=OLD_ID, canonical_property_id=CANONICAL_ID, actor=ACTOR,
        )
    assert result["ok"] is True
    assert all(result["checks"].values())
    conn.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollback_stops_when_fresh_measurement_differs():
    cursor = Cursor(mapping=mapping_row())
    conn = Connection(cursor)
    with (
        patch.object(maintenance, "validate_database_scope"),
        patch.object(maintenance, "_verify_manifest", return_value={"run_id": RUN_ID}),
        patch.object(maintenance, "open_connection", AsyncMock(return_value=conn)),
        patch.object(maintenance, "close_connection", AsyncMock()),
        patch.object(maintenance, "_check_scope", AsyncMock()),
        patch.object(maintenance, "_require_maintenance_schema", AsyncMock()),
        patch.object(maintenance, "_lock", AsyncMock()),
        patch.object(maintenance, "_unlock", AsyncMock()),
        patch.object(maintenance, "_measure_cursor", AsyncMock(return_value=measurement("d" * 64))),
    ):
        with pytest.raises(maintenance.IdentityMaintenanceError, match="rollback_state_changed"):
            await maintenance.run_rollback(
                run_id=RUN_ID, old_property_id=OLD_ID, canonical_property_id=CANONICAL_ID,
                measurement_hash=POST_HASH, backup_manifest="manifest", confirmation=maintenance.CONFIRM_ROLLBACK,
                actor=ACTOR,
            )
    assert not any(sql.startswith("UPDATE") for sql, _ in cursor.calls)
    conn.commit.assert_not_awaited()
    conn.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollback_restores_mapping_state_atomically_without_touching_other_tables():
    cursor = Cursor(mapping=mapping_row())
    conn = Connection(cursor)
    restored_summary = {
        "blocking_reference_count": 0, "old_certificate_count": 0,
        "old_certificate_source_count": 0, "old_small_community_link_count": 0,
        "canonical_certificate_count": 0, "canonical_certificate_source_count": 0,
    }
    with (
        patch.object(maintenance, "validate_database_scope"),
        patch.object(maintenance, "_verify_manifest", return_value={"run_id": RUN_ID}),
        patch.object(maintenance, "open_connection", AsyncMock(return_value=conn)),
        patch.object(maintenance, "close_connection", AsyncMock()),
        patch.object(maintenance, "_check_scope", AsyncMock()),
        patch.object(maintenance, "_require_maintenance_schema", AsyncMock()),
        patch.object(maintenance, "_lock", AsyncMock()),
        patch.object(maintenance, "_unlock", AsyncMock()),
        patch.object(maintenance, "_measure_cursor", AsyncMock(side_effect=[
            measurement(POST_HASH, summary=restored_summary),
            measurement(MEASURE_HASH, summary=restored_summary),
        ])),
        patch.object(maintenance, "_mapping_events_match", AsyncMock(return_value=True)),
        patch.object(maintenance, "_certificate_audit_matches", AsyncMock(return_value=True)),
    ):
        result = await maintenance.run_rollback(
            run_id=RUN_ID, old_property_id=OLD_ID, canonical_property_id=CANONICAL_ID,
            measurement_hash=POST_HASH, backup_manifest="manifest", confirmation=maintenance.CONFIRM_ROLLBACK,
            actor=ACTOR,
        )
    assert result["idempotent"] is False
    assert result["measurement_hash"] == MEASURE_HASH
    assert any("status='revoked'" in sql for sql, _ in cursor.calls)
    assert not any("registry_property_certificates SET property_id" in sql for sql, _ in cursor.calls)
    conn.commit.assert_awaited_once()
    conn.rollback.assert_not_awaited()


def test_backup_path_must_stay_under_fixed_root(tmp_path):
    root = tmp_path / "fixed"
    root.mkdir()
    outside = tmp_path / "backup-manifest.json"
    outside.write_text("{}", encoding="utf-8")
    with patch.object(maintenance, "BACKUP_ROOT", root):
        with pytest.raises(maintenance.IdentityMaintenanceError, match="backup_manifest_outside_fixed_root"):
            maintenance._verify_manifest(str(outside), run_id=RUN_ID)
