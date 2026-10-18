import json
from argparse import Namespace
import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.registry_identity_evidence import (
    discover_reference_inventory,
    scan_certificate_source_references,
    scan_references,
    seal_artifact,
    verify_artifact,
)
from migrations import registry_property_identity_evidence as migration


class Cursor:
    def __init__(self, *, unknown_entity_type=False):
        self.sql = ""
        self.calls = []
        self.unknown_entity_type = unknown_entity_type

    async def execute(self, sql, params=()):
        self.sql = sql
        self.calls.append((sql, params))

    async def fetchall(self):
        if self.sql.startswith("SELECT c.TABLE_SCHEMA"):
            return [
                ("RegistryData", "registry_properties", "id", "bigint"),
                ("RegistryData", "registry_property_certificates", "property_id", "bigint"),
                ("RegistryData", "registry_property_certificates", "id", "bigint"),
                ("RegistryData", "registry_source_records", "entity_id", "bigint"),
                ("RegistryData", "registry_source_records", "entity_type", "varchar"),
                ("RegistryData", "registry_change_candidates", "entity_id", "bigint"),
                ("RegistryData", "registry_change_candidates", "entity_type", "varchar"),
                ("OnlineData", "task_links", "registration_property_id", "bigint"),
                ("OnlineData", "people", "property_manager_id", "bigint"),
            ]
        if "CONSTRAINT_NAME='PRIMARY'" in self.sql:
            return [
                ("RegistryData", "registry_properties", "id"),
                ("RegistryData", "registry_property_certificates", "id"),
                ("RegistryData", "registry_source_records", "id"),
                ("RegistryData", "registry_change_candidates", "id"),
                ("OnlineData", "task_links", "id"),
            ]
        if "REFERENCED_TABLE_NAME='registry_properties'" in self.sql:
            return []
        if self.sql.startswith("SELECT entity_type,COUNT(*)"):
            if self.unknown_entity_type:
                return [("future_type", 2)]
            return [("household_property", 1), ("property_certificate", 1)]
        if self.sql.startswith("SELECT certificate.property_id"):
            return [(7, 201, 101)]
        if "FROM `RegistryData`.`registry_property_certificates`" in self.sql:
            return [(7, 101), (7, 102)]
        if "FROM `RegistryData`.`registry_source_records`" in self.sql:
            return [(7, 201)]
        if "FROM `OnlineData`.`task_links`" in self.sql:
            return [(7, 301)]
        return []


@pytest.mark.asyncio
async def test_inventory_covers_direct_and_typed_polymorphic_references():
    cursor = Cursor()
    inventory, opaque, warnings = await discover_reference_inventory(
        cursor, ["RegistryData", "OnlineData"], "RegistryData"
    )
    assert opaque == []
    assert warnings == []
    assert {
        (item["schema"], item["table"], item["column"], item["kind"], item["entity_type"])
        for item in inventory
    } == {
        ("OnlineData", "task_links", "registration_property_id", "direct", None),
        ("RegistryData", "registry_property_certificates", "property_id", "direct", None),
        ("RegistryData", "registry_source_records", "entity_id", "historical_polymorphic", "household_property"),
        ("RegistryData", "registry_source_records", "entity_id", "historical_polymorphic", "property"),
        ("RegistryData", "registry_change_candidates", "entity_id", "polymorphic", "property"),
    }
    assert not any(item["column"] == "property_manager_id" for item in inventory)


@pytest.mark.asyncio
async def test_unknown_entity_type_is_an_explicit_blocking_warning():
    cursor = Cursor(unknown_entity_type=True)
    _, _, warnings = await discover_reference_inventory(
        cursor, ["RegistryData", "OnlineData"], "RegistryData"
    )
    assert warnings == [{
        "schema": "RegistryData",
        "table": "registry_change_candidates",
        "column": "entity_id",
        "entity_type": "future_type",
        "count": 2,
        "warning": "unknown_entity_type_blocks_review",
    }, {
        "schema": "RegistryData",
        "table": "registry_source_records",
        "column": "entity_id",
        "entity_type": "future_type",
        "count": 2,
        "warning": "unknown_entity_type_blocks_review",
    }]


@pytest.mark.asyncio
async def test_reference_scan_adds_discriminator_and_only_returns_digests():
    cursor = Cursor()
    inventory, _, _ = await discover_reference_inventory(
        cursor, ["RegistryData", "OnlineData"], "RegistryData"
    )
    findings = await scan_references(cursor, inventory, [7])
    assert {(item["table"], item["entity_type"], item["count"]) for item in findings} == {
        ("registry_property_certificates", None, 2),
        ("registry_source_records", "household_property", 1),
        ("registry_source_records", "property", 1),
        ("task_links", None, 1),
    }
    assert all("payload" not in json.dumps(item) for item in findings)
    source_query = next(sql for sql, _ in cursor.calls
                        if "registry_source_records`" in sql and "`entity_type`=%s" in sql)
    assert "`entity_type`=%s" in source_query
    source_params = next(params for sql, params in cursor.calls
                         if "registry_source_records`" in sql and "`entity_type`=%s" in sql)
    assert source_params[-1] == "household_property"


@pytest.mark.asyncio
async def test_source_records_property_entity_type_is_scanned():
    cursor = Cursor()
    inventory, _, _ = await discover_reference_inventory(
        cursor, ["RegistryData"], "RegistryData"
    )
    property_inventory = [
        item for item in inventory
        if item["table"] == "registry_source_records" and item["entity_type"] == "property"
    ]
    assert property_inventory
    findings = await scan_references(cursor, property_inventory, [7])
    assert findings and findings[0]["property_id"] == 7


@pytest.mark.asyncio
async def test_certificate_source_records_are_resolved_through_certificate_id():
    cursor = Cursor()
    references = await scan_certificate_source_references(
        cursor, registry_schema="RegistryData", property_ids=[7]
    )
    assert references == [{
        "property_id": 7,
        "schema": "RegistryData",
        "table": "registry_source_records",
        "column": "entity_id",
        "kind": "certificate_source_reference",
        "entity_type": "property_certificate",
        "count": 1,
        "record_key_sha256": references[0]["record_key_sha256"],
        "stable_record_keys": True,
    }]


@pytest.mark.asyncio
async def test_reference_scan_includes_canonical_property_for_certificate_conflict():
    class CanonicalCursor(Cursor):
        async def fetchall(self):
            if self.sql.startswith("SELECT id,community_id,street,natural_address"):
                return [
                    (7, 1, "", "", "", "", "同址", "", None, "", "", "", "household", "active", 1),
                    (8, 1, "", "", "", "", "同址", "", None, "", "", "", "household", "active", 1),
                ]
            return await super().fetchall()

        async def fetchone(self):
            return None

    cursor = CanonicalCursor()
    with patch.object(migration, "plan_identity_merges", return_value=([(7, 8)], 0)), \
         patch.object(migration, "scan_references", AsyncMock(return_value=[{
             "property_id": 7, "schema": "RegistryData", "table": "registry_property_certificates",
             "column": "property_id", "kind": "direct", "count": 1,
             "record_key_sha256": "old", "stable_record_keys": True,
         }, {
             "property_id": 8, "schema": "RegistryData", "table": "registry_property_certificates",
             "column": "property_id", "kind": "direct", "count": 1,
             "record_key_sha256": "current", "stable_record_keys": True,
         }])), \
         patch.object(migration, "scan_certificate_source_references", AsyncMock(return_value=[{
             "property_id": 7, "schema": "RegistryData", "table": "registry_source_records",
             "column": "entity_id", "kind": "certificate_source_reference", "entity_type": "property_certificate",
             "count": 1, "record_key_sha256": "source-old", "stable_record_keys": True,
         }, {
             "property_id": 8, "schema": "RegistryData", "table": "registry_source_records",
             "column": "entity_id", "kind": "certificate_source_reference", "entity_type": "property_certificate",
             "count": 1, "record_key_sha256": "source-current", "stable_record_keys": True,
         }])):
        artifact = await migration.build_evidence(
            cursor, ["RegistryData"], "RegistryData", [], "run-test"
        )
    assert artifact["candidates"][0]["canonical_certificate_count"] == 1
    assert "certificate_migration_not_unique" in artifact["candidates"][0]["blockers"]


@pytest.mark.asyncio
async def test_certificate_exception_is_checked_across_all_old_properties_for_canonical():
    class GroupCursor:
        def __init__(self):
            self.sql = ""

        async def execute(self, sql, params=()):
            self.sql = sql

        async def fetchall(self):
            if self.sql.startswith("SELECT id,community_id,street,natural_address"):
                return [
                    (7, 1, "", "", "", "", "同址", "", None, "", "", "", "household", "active", 1),
                    (8, 1, "", "", "", "", "同址", "", None, "", "", "", "household", "active", 1),
                    (9, 1, "", "", "", "", "同址", "", None, "", "", "", "household", "active", 1),
                ]
            return []

        async def fetchone(self):
            return None

    cursor = GroupCursor()
    with patch.object(migration, "plan_identity_merges", return_value=([(7, 9), (8, 9)], 0)), \
         patch.object(migration, "scan_references", AsyncMock(return_value=[
             {"property_id": 7, "schema": "RegistryData", "table": "registry_property_certificates",
              "column": "property_id", "kind": "direct", "count": 1, "record_key_sha256": "old-7",
              "stable_record_keys": True},
             {"property_id": 8, "schema": "RegistryData", "table": "registry_property_certificates",
              "column": "property_id", "kind": "direct", "count": 1, "record_key_sha256": "old-8",
              "stable_record_keys": True},
         ])), \
         patch.object(migration, "scan_certificate_source_references", AsyncMock(return_value=[
             {"property_id": 7, "schema": "RegistryData", "table": "registry_source_records", "column": "entity_id",
              "kind": "certificate_source_reference", "entity_type": "property_certificate", "count": 1,
              "record_key_sha256": "source-7", "stable_record_keys": True},
             {"property_id": 8, "schema": "RegistryData", "table": "registry_source_records", "column": "entity_id",
              "kind": "certificate_source_reference", "entity_type": "property_certificate", "count": 1,
              "record_key_sha256": "source-8", "stable_record_keys": True},
         ])):
        artifact = await migration.build_evidence(cursor, ["RegistryData"], "RegistryData", [], "run-test")
    assert len(artifact["candidates"]) == 2
    assert all("certificate_migration_not_unique" in candidate["blockers"] for candidate in artifact["candidates"])
    assert all(candidate["group_old_certificate_count"] == 2 for candidate in artifact["candidates"])


def test_artifact_hash_is_self_verifying():
    artifact = seal_artifact({"schema_version": 1, "references": [], "read_only": True})
    assert verify_artifact(artifact)
    tampered = {**artifact, "references": [{"count": 1}]}
    assert not verify_artifact(tampered)


def test_query_budget_rejects_unbounded_values():
    with pytest.raises(ValueError, match="invalid_max_execution_time"):
        migration.validate_query_budget(Namespace(max_execution_ms=0, lock_wait_timeout=2))
    with pytest.raises(ValueError, match="invalid_lock_wait_timeout"):
        migration.validate_query_budget(Namespace(max_execution_ms=30000, lock_wait_timeout=30))


def test_evidence_output_never_overwrites_an_existing_file(tmp_path):
    target = tmp_path / "evidence.json"
    target.write_text("previous evidence", encoding="utf-8")
    with pytest.raises(FileExistsError):
        migration.write_exclusive(str(target), {"artifact_sha256": "new"})
    assert target.read_text(encoding="utf-8") == "previous evidence"


@pytest.mark.asyncio
async def test_command_enforces_read_only_snapshot_and_always_rolls_back():
    args = Namespace(environment="production", property_id=[], output=None,
                     max_execution_ms=30000, lock_wait_timeout=2)
    cursor = AsyncMock()
    cursor.fetchall.return_value = [(name,) for name in migration.database_scope()]
    conn = AsyncMock()
    conn.cursor = lambda: conn
    conn.__aenter__.return_value = cursor
    close = AsyncMock()
    with patch.object(migration, "open_connection", AsyncMock(return_value=conn)), \
         patch.object(migration, "close_connection", close), \
         patch.object(migration, "build_evidence", AsyncMock(return_value={"read_only": True})), \
         patch.object(migration.settings, "APP_ENVIRONMENT", "production"), \
         patch.object(migration, "validate_database_scope"):
        artifact = await migration.run(args)
    sql = [call.args[0] for call in cursor.execute.await_args_list]
    assert sql[:5] == ["SET SESSION TRANSACTION READ ONLY", "SET SESSION MAX_EXECUTION_TIME=%s",
                       "SET SESSION lock_wait_timeout=%s", "SET SESSION innodb_lock_wait_timeout=%s",
                       "START TRANSACTION WITH CONSISTENT SNAPSHOT"]
    assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
                   for statement in sql)
    assert verify_artifact(artifact)
    conn.rollback.assert_awaited_once()
    conn.commit.assert_not_awaited()
    close.assert_awaited_once_with(conn)


@pytest.mark.asyncio
async def test_selected_measurement_keeps_cross_community_address_blocking():
    class EvidenceCursor:
        def __init__(self):
            self.sql = ""
            self.calls = []

        async def execute(self, sql, params=()):
            self.sql = sql
            self.calls.append((sql, params))

        async def fetchall(self):
            if self.sql.startswith("SELECT id,community_id,street,natural_address"):
                return [
                    (7, 1, "", "", "", "", "同址", "", None, "", "", "", "household", "active", 1),
                    (8, 1, "", "", "", "", "同址", "", None, "", "", "", "household", "active", 1),
                    (9, 2, "", "", "", "", "同址", "", None, "", "", "", "household", "active", 1),
                    (10, 2, "", "", "", "", "同址", "", None, "", "", "", "household", "active", 1),
                ]
            if "FROM registry_property_small_community_links" in self.sql:
                return []
            return []

        async def fetchone(self):
            return None

    cursor = EvidenceCursor()
    with patch.object(migration, "plan_identity_merges", return_value=([(7, 8)], 0)):
        artifact = await migration.build_evidence(
            cursor, ["RegistryData"], "RegistryData", [7], "run-test"
        )
    assert artifact["cross_community_identity_blocked"] is True
    assert artifact["cross_community_address_group_count"] == 1
    assert artifact["candidates"] == [
        {
            "old_property_id": 7,
            "canonical_property_id": 8,
            "disposition": "blocked",
            "reference_disposition": "no_references_candidate",
            "blockers": ["cross_community_address"],
            "reference_count": 0,
            "old_certificate_count": 0,
            "old_certificate_source_count": 0,
            "canonical_certificate_count": 0,
            "group_old_certificate_count": 0,
            "group_old_certificate_source_count": 0,
        }
    ]


@pytest.mark.asyncio
async def test_evidence_queries_do_not_write():
    cursor = Cursor()
    inventory, _, _ = await discover_reference_inventory(
        cursor, ["RegistryData", "OnlineData"], "RegistryData"
    )
    await scan_references(cursor, inventory, [7])
    assert all(not sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE"))
               for sql, _ in cursor.calls)
