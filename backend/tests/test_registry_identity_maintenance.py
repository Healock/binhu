from unittest.mock import AsyncMock, patch
from argparse import Namespace
import os

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.registry_identity_maintenance import plan_identity_merges, prepare_merges
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



def test_executor_and_legacy_write_options_are_not_available():
    from services import registry_identity_maintenance as service
    assert not hasattr(service, "apply_merge")
    for argv in (
        ["migrate", "--apply", "--environment", "production"],
        ["measure", "--apply", "--environment", "production"],
        ["measure", "--backup-reference", "anything", "--environment", "production"],
        ["measure", "--plan-token", "anything", "--environment", "production"],
    ):
        with pytest.raises(SystemExit):
            migration.parser().parse_args(argv)


@pytest.mark.asyncio
async def test_read_only_guard_rejects_write_command_and_environment_before_connect():
    args = Namespace(command="migrate", environment=migration.settings.APP_ENVIRONMENT, property_id=[1])
    with patch.object(migration, "open_connection", AsyncMock()) as connect:
        with pytest.raises(ValueError, match="read_only_commands_only"):
            await migration.run(args)
        args.command = "measure"
        args.environment = "staging" if args.environment != "staging" else "production"
        with pytest.raises(ValueError, match="identity_mismatch"):
            await migration.run(args)
        connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_measurement_is_unlocked_and_hash_includes_database_scope():
    cur = Cursor()
    _, _, first = await prepare_merges(cur, schemas=["RegistryData"], registry_schema="RegistryData", property_ids=[])
    _, _, second = await prepare_merges(cur, schemas=["RegistryData", "OnlineData"], registry_schema="RegistryData", property_ids=[])
    assert first != second
    assert all(sql.startswith("SELECT") and "FOR UPDATE" not in sql for sql, _ in cur.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["measure", "verify"])
@pytest.mark.parametrize("failure", [None, RuntimeError, __import__("asyncio").CancelledError])
async def test_read_only_snapshot_always_rolls_back_and_closes(command, failure):
    args = Namespace(command=command, environment=migration.settings.APP_ENVIRONMENT, property_id=[1])
    cursor = AsyncMock()
    cursor.fetchall.return_value = [(name,) for name in migration.database_scope()]
    conn = AsyncMock()
    conn.cursor = lambda: conn
    conn.__aenter__.return_value = cursor
    prepare = AsyncMock(return_value=([(2, 1)], 0, "hash"),
                        side_effect=failure("synthetic") if failure else None)
    close = AsyncMock()
    with patch.object(migration, "open_connection", AsyncMock(return_value=conn)), \
         patch.object(migration, "close_connection", close), \
         patch.object(migration, "prepare_merges", prepare), \
         patch.object(migration.settings, "APP_ENVIRONMENT", "production"), \
         patch.object(migration, "validate_database_scope"):
        args.environment = "production"
        if failure:
            with pytest.raises(failure):
                await migration.run(args)
        else:
            result = await migration.run(args)
            assert result["read_only"] and not result["execution_available"]
            assert result.get("verified", False) is False
        sql = [call.args[0] for call in cursor.execute.await_args_list]
        assert sql[:2] == ["SET SESSION TRANSACTION READ ONLY", "START TRANSACTION WITH CONSISTENT SNAPSHOT"]
        assert not any(statement.startswith(("UPDATE", "DELETE", "INSERT", "CREATE", "ALTER", "DROP")) for statement in sql)
        conn.rollback.assert_awaited_once()
        conn.commit.assert_not_awaited()
        close.assert_awaited_once_with(conn)
