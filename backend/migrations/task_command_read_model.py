"""Create the immutable online-task command ledger and encrypted event field."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json

import aiomysql

from config import settings
from services.domain_events import ensure_outbox_schema_sql


COMMAND_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `_task_command_records` (
    command_id CHAR(36) NOT NULL PRIMARY KEY,
    operation_id CHAR(36) NOT NULL,
    event_id CHAR(36) NOT NULL,
    actor_id BIGINT UNSIGNED NOT NULL,
    environment VARCHAR(20) NOT NULL,
    parser_type VARCHAR(80) NOT NULL,
    task_id VARCHAR(160) NOT NULL,
    source_id BIGINT UNSIGNED NOT NULL,
    action VARCHAR(40) NOT NULL,
    expected_revision BIGINT UNSIGNED DEFAULT NULL,
    revision BIGINT UNSIGNED NOT NULL,
    changed_fields_json JSON NOT NULL,
    payload_ciphertext_sha256 CHAR(64) NOT NULL,
    authorization_policy VARCHAR(80) NOT NULL,
    authorization_scope_sha256 CHAR(64) NOT NULL,
    authorization_decision VARCHAR(8) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_task_command_operation (operation_id),
    UNIQUE KEY uq_task_command_event (event_id),
    INDEX idx_task_command_revision (task_id, source_id, revision),
    INDEX idx_task_command_created (created_at, command_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
"""

REQUIRED_OUTBOX_COLUMNS = {
    "event_id", "schema_version", "command_id", "task_id", "source_id", "operation_id",
    "changed_fields_json", "payload_envelope_json", "status", "occurred_at",
}
REQUIRED_COMMAND_COLUMNS = {
    "command_id", "operation_id", "event_id", "actor_id", "environment",
    "parser_type", "task_id", "source_id", "action", "expected_revision",
    "revision", "changed_fields_json", "payload_ciphertext_sha256",
    "authorization_policy", "authorization_scope_sha256",
    "authorization_decision", "created_at",
}
REQUIRED_OUTBOX_INDEXES = {"idx_domain_event_retention"}


def schema_status(database: str, schema: dict) -> dict:
    """Return a deterministic migration status without exposing DB contents."""
    outbox_columns = set(schema["columns"].get("_domain_event_outbox", ()))
    command_columns = set(schema["columns"].get("_task_command_records", ()))
    outbox_missing = sorted(REQUIRED_OUTBOX_COLUMNS - outbox_columns)
    index_missing = sorted(REQUIRED_OUTBOX_INDEXES - set(schema.get("indexes", ())))
    command_missing = (
        sorted(REQUIRED_COMMAND_COLUMNS - command_columns)
        if database == settings.MYSQL_ONLINE_DATA_DB else []
    )
    return {
        **schema,
        "missing": {
            "outbox_columns": outbox_missing,
            "outbox_indexes": index_missing,
            "command_columns": command_missing,
        },
        "consistent": not (outbox_missing or index_missing or command_missing),
    }


async def close_connection(conn) -> None:
    conn.close()
    waiter = getattr(conn, "wait_closed", None)
    if waiter:
        result = waiter()
        if inspect.isawaitable(result):
            await result


async def open_connection(database: str):
    return await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=database,
        autocommit=True,
        charset="utf8mb4",
    )


async def inspect_schema(cur, database: str) -> dict:
    await cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema=DATABASE() AND table_name IN "
        "('_domain_event_outbox','_task_command_records')"
    )
    tables = {str(row[0]) for row in await cur.fetchall()}
    columns = {}
    for table in ("_domain_event_outbox", "_task_command_records"):
        if table not in tables:
            columns[table] = []
            continue
        await cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=%s ORDER BY ordinal_position",
            (table,),
        )
        columns[table] = [str(row[0]) for row in await cur.fetchall()]
    indexes = set()
    if "_domain_event_outbox" in tables:
        await cur.execute(
            "SELECT DISTINCT index_name FROM information_schema.statistics "
            "WHERE table_schema=DATABASE() AND table_name='_domain_event_outbox'"
        )
        indexes = {str(row[0]) for row in await cur.fetchall()}
    return {
        "database": database,
        "tables": sorted(tables),
        "columns": columns,
        "indexes": sorted(indexes),
    }


async def run(command: str, apply: bool) -> dict:
    databases = [settings.MYSQL_ONLINE_DATA_DB, settings.MYSQL_PLATFORM_DB]
    results = []
    for database in databases:
        conn = await open_connection(database)
        try:
            async with conn.cursor() as cur:
                if command == "migrate" and apply:
                    await cur.execute("SET SESSION lock_wait_timeout=5")
                    await cur.execute(ensure_outbox_schema_sql())
                    before = await inspect_schema(cur, database)
                    existing = set(before["columns"]["_domain_event_outbox"])
                    additions = (
                        ("command_id", "CHAR(36) DEFAULT NULL"),
                        ("task_id", "VARCHAR(160) DEFAULT NULL"),
                        ("source_id", "BIGINT UNSIGNED DEFAULT NULL"),
                        ("operation_id", "CHAR(36) DEFAULT NULL"),
                        ("changed_fields_json", "JSON DEFAULT NULL"),
                        ("payload_envelope_json", "JSON DEFAULT NULL"),
                    )
                    for name, definition in additions:
                        if name not in existing:
                            await cur.execute(
                                f"ALTER TABLE `_domain_event_outbox` "
                                f"ADD COLUMN `{name}` {definition}"
                            )
                    if "idx_domain_event_retention" not in before["indexes"]:
                        await cur.execute(
                            "ALTER TABLE `_domain_event_outbox` "
                            "ADD INDEX idx_domain_event_retention (status,published_at,event_id)"
                        )
                    if database == settings.MYSQL_ONLINE_DATA_DB:
                        await cur.execute(COMMAND_TABLE_SQL)
                        before = await inspect_schema(cur, database)
                        command_existing = set(before["columns"]["_task_command_records"])
                        command_additions = (
                            ("authorization_policy", "VARCHAR(80) NOT NULL DEFAULT 'legacy_unverified'"),
                            ("authorization_scope_sha256", "CHAR(64) NOT NULL DEFAULT "
                             "'0000000000000000000000000000000000000000000000000000000000000000'"),
                            ("authorization_decision", "VARCHAR(8) NOT NULL DEFAULT 'allow'"),
                        )
                        for name, definition in command_additions:
                            if name not in command_existing:
                                await cur.execute(
                                    f"ALTER TABLE `_task_command_records` "
                                    f"ADD COLUMN `{name}` {definition}"
                                )
                result = await inspect_schema(cur, database)
                result = schema_status(database, result)
                result["command"] = command
                result["apply"] = apply
                results.append(result)
        finally:
            await close_connection(conn)
    return {"command": command, "apply": apply, "databases": results,
            "consistent": all(item["consistent"] for item in results)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--apply", action="store_true")
    sub.add_parser("verify")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    result = await run(args.command, getattr(args, "apply", False))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "verify" and not result["consistent"]:
        raise SystemExit("task_command_read_model_inconsistent")


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
