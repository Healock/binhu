from unittest.mock import AsyncMock, patch
from argparse import Namespace
import os

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.registry_identity_maintenance import plan_identity_merges, prepare_merges, apply_merge
from migrations import registry_property_identity as migration


def prop(identifier, number, status, when, **changes):
    row = [identifier, 8, "", "合成路8号", "", "", "合成路8号", number,
           when, "个人出租", "", status, "household", "active", 1]
    for index, value in changes.items():
        row[int(index)] = value
    return tuple(row)


ROWS = [prop(1, "NEW", "未注销", "2026-01-01"), prop(2, "OLD", "已注销", "2020-01-01")]


def test_only_proven_registration_history_is_mergeable():
    assert plan_identity_merges(ROWS) == ([(2, 1)], 0)
    for changes in ({"11": "未注销"}, {"8": None}, {"8": "2026-01-01"},
                    {"12": "manual"}, {"13": "inactive"}, {"3": "合成路8号旁"}):
        assert plan_identity_merges([ROWS[0], prop(2, "OLD", "已注销", "2020-01-01", **changes)]) == ([], 1)
    assert plan_identity_merges([ROWS[0]]) == ([], 0)


class Cursor:
    def __init__(self, *, links=(), foreign=False):
        self.links = links
        self.foreign = foreign
        self.calls = []
        self.description = [("id",), ("community_id",)]

    async def execute(self, sql, params=()):
        self.sql = sql
        self.params = params
        self.calls.append((sql, params))

    async def fetchall(self):
        if "source_house_no,source_updated_at" in self.sql:
            return ROWS
        if "information_schema.COLUMNS" in self.sql:
            return [("OnlineData", "_task_registration_links", "property_id")] if self.foreign else []
        if "SELECT property_id,small_community_id" in self.sql:
            return self.links
        if "SELECT id FROM registry_property_address_versions" in self.sql:
            return [(21,), (22,)]
        return []

    async def fetchone(self):
        if "_task_registration_links" in self.sql:
            return (1,)
        if self.sql.startswith("SELECT * FROM registry_properties"):
            return (2, 8)
        if "MAX(version_no)" in self.sql:
            return (5,)
        return None


@pytest.mark.asyncio
async def test_measurement_blocks_cross_domain_references_and_human_confirmations():
    for cursor in (Cursor(foreign=True), Cursor(links=[(2, 7, "confirmed", 9, "2026-01-01")]),
                   Cursor(links=[(2, 7, "suggested", None, None), (1, 8, "suggested", None, None)])):
        ready, blocked, token = await prepare_merges(cursor, schemas=["RegistryData", "OnlineData"], registry_schema="RegistryData", property_ids=[1])
        assert ready == [] and blocked == 1 and len(token) == 64
        assert not any(sql.startswith(("UPDATE", "INSERT", "DELETE")) for sql, _ in cursor.calls)


@pytest.mark.asyncio
async def test_apply_archives_before_removing_and_preserves_source_and_address_ids():
    cursor = Cursor()
    await apply_merge(cursor, old_id=2, current_id=1, run_id="synthetic-run")
    statements = [sql for sql, _ in cursor.calls]
    archive = next(i for i, sql in enumerate(statements) if sql.startswith("INSERT INTO registry_property_identity_merges"))
    removal = next(i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM registry_properties"))
    assert archive < removal
    assert any("UPDATE registry_source_records SET entity_id" in sql for sql in statements)
    versions = [params for sql, params in cursor.calls if sql.startswith("UPDATE registry_property_address_versions SET property_id")]
    assert versions == [(1, 6, 21), (1, 7, 22)]
    assert not any("DELETE FROM registry_source_records" in sql or "DELETE FROM registry_property_address_versions" in sql for sql in statements)


@pytest.mark.asyncio
async def test_apply_guard_rejects_environment_and_missing_backup_before_connect():
    args = Namespace(command="migrate", apply=True, environment=migration.settings.APP_ENVIRONMENT,
                     property_id=[1], plan_token="a" * 64, backup_reference=None)
    with patch.object(migration, "open_connection", AsyncMock()) as connect:
        with pytest.raises(ValueError, match="backup_reference"):
            await migration.run(args)
        args.environment = "staging" if args.environment != "staging" else "production"
        with pytest.raises(ValueError, match="identity_mismatch"):
            await migration.run(args)
        connect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed,fail_apply", [(True, False), (False, True)])
async def test_changed_measurement_or_apply_failure_rolls_back(tmp_path, changed, fail_apply):
    backup = tmp_path / 'synthetic-backup-evidence.json'
    backup.write_text('{}')
    args = Namespace(command="migrate", apply=True, environment=migration.settings.APP_ENVIRONMENT,
                     property_id=[1], plan_token="approved", backup_reference=str(backup))
    cursor = AsyncMock()
    cursor.fetchall.return_value = [(name,) for name in migration.database_scope()]
    conn = AsyncMock()
    conn.cursor = lambda: conn
    conn.__aenter__.return_value = cursor
    with patch.object(migration, "open_connection", AsyncMock(return_value=conn)), \
         patch.object(migration, "close_connection", AsyncMock()), \
         patch.object(migration, "prepare_merges", AsyncMock(return_value=([(2, 1)], 0, "changed" if changed else "approved"))), \
         patch.object(migration, "apply_merge", AsyncMock(side_effect=RuntimeError('synthetic_failure') if fail_apply else None)) as apply:
        with pytest.raises((ValueError, RuntimeError)):
            await migration.run(args)
        conn.rollback.assert_awaited()
        conn.commit.assert_not_awaited()
        if changed:
            apply.assert_not_awaited()
