import json
from argparse import Namespace
import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.registry_identity_evidence import (
    discover_reference_inventory,
    scan_references,
    seal_artifact,
    verify_artifact,
)
from migrations import registry_property_identity_evidence as migration


class Cursor:
    def __init__(self):
        self.sql = ""
        self.calls = []

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
    inventory, opaque = await discover_reference_inventory(
        cursor, ["RegistryData", "OnlineData"], "RegistryData"
    )
    assert opaque == []
    assert {
        (item["schema"], item["table"], item["column"], item["kind"], item["entity_type"])
        for item in inventory
    } == {
        ("OnlineData", "task_links", "registration_property_id", "direct", None),
        ("RegistryData", "registry_property_certificates", "property_id", "direct", None),
        ("RegistryData", "registry_source_records", "entity_id", "historical_polymorphic", "household_property"),
        ("RegistryData", "registry_change_candidates", "entity_id", "polymorphic", "household_property"),
    }


@pytest.mark.asyncio
async def test_reference_scan_adds_discriminator_and_only_returns_digests():
    cursor = Cursor()
    inventory, _ = await discover_reference_inventory(
        cursor, ["RegistryData", "OnlineData"], "RegistryData"
    )
    findings = await scan_references(cursor, inventory, [7])
    assert {(item["table"], item["count"]) for item in findings} == {
        ("registry_property_certificates", 2),
        ("registry_source_records", 1),
        ("task_links", 1),
    }
    assert all("payload" not in json.dumps(item) for item in findings)
    source_query = next(sql for sql, _ in cursor.calls if "registry_source_records`" in sql)
    assert "`entity_type`=%s" in source_query
    source_params = next(params for sql, params in cursor.calls if "registry_source_records`" in sql)
    assert source_params[-1] == "household_property"


def test_artifact_hash_is_self_verifying():
    artifact = seal_artifact({"schema_version": 1, "references": [], "read_only": True})
    assert verify_artifact(artifact)
    tampered = {**artifact, "references": [{"count": 1}]}
    assert not verify_artifact(tampered)


def test_evidence_output_never_overwrites_an_existing_file(tmp_path):
    target = tmp_path / "evidence.json"
    target.write_text("previous evidence", encoding="utf-8")
    with pytest.raises(FileExistsError):
        migration.write_exclusive(str(target), {"artifact_sha256": "new"})
    assert target.read_text(encoding="utf-8") == "previous evidence"


@pytest.mark.asyncio
async def test_command_enforces_read_only_snapshot_and_always_rolls_back():
    args = Namespace(environment="production", property_id=[], output=None, run_id=None)
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
    assert sql[:2] == ["SET SESSION TRANSACTION READ ONLY", "START TRANSACTION WITH CONSISTENT SNAPSHOT"]
    assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
                   for statement in sql)
    assert verify_artifact(artifact)
    conn.rollback.assert_awaited_once()
    conn.commit.assert_not_awaited()
    close.assert_awaited_once_with(conn)


@pytest.mark.asyncio
async def test_evidence_queries_do_not_write():
    cursor = Cursor()
    inventory, _ = await discover_reference_inventory(
        cursor, ["RegistryData", "OnlineData"], "RegistryData"
    )
    await scan_references(cursor, inventory, [7])
    assert all(not sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE"))
               for sql, _ in cursor.calls)
