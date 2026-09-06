"""Seed the isolated business Kafka shadow with the shared fictional fixture.

This entry point deliberately has its own fail-closed checks. The historical
load-test stack targets a different database/source-reference contract and is
not reused here.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import aiomysql
import bcrypt

sys.path.insert(0, "/app")
sys.path.insert(0, "/load-tests")

from database import close_db, db_manager, init_db  # noqa: E402
from config import settings  # noqa: E402
from fixture import make_tasks, make_users, password_hint  # noqa: E402
from services.local_source import create_local_source_row  # noqa: E402
from services.online_source import rebuild_projection  # noqa: E402
from services.parsers import get_parser  # noqa: E402
from services.task_registration import select_registration_property  # noqa: E402
from services.task_outbox_bridge import enqueue_task_event  # noqa: E402
from services.task_workflow import TASK_WORKFLOWS  # noqa: E402


RUN_ID_RE = re.compile(r"^KSHADOW-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
DATABASE_RE = re.compile(r"^KShadow_[A-Za-z0-9_]{1,50}$")
MAX_BATCH_SIZE = 100
EXPECTATION_TABLE = "_shadow_business_expectations"


class SeedSafetyError(RuntimeError):
    """A precondition failed before business fixture writes were allowed."""


@dataclass(frozen=True)
class ShadowContext:
    run_id: str
    host: str
    port: int
    user: str
    password: str
    online_db: str
    archive_db: str
    daily_db: str

    @property
    def databases(self) -> tuple[str, str, str]:
        return self.online_db, self.archive_db, self.daily_db


def _required(environ: Mapping[str, str], name: str) -> str:
    value = str(environ.get(name, "") or "").strip()
    if not value:
        raise SeedSafetyError(f"missing {name}")
    return value


def validate_context(
    run_id: str,
    environ: Mapping[str, str] | None = None,
) -> ShadowContext:
    """Validate the complete business-shadow target before opening a pool."""
    env = environ if environ is not None else os.environ
    requested_run_id = str(run_id or "").strip()
    if str(env.get("APP_ENVIRONMENT", "")).strip().lower() != "shadow":
        raise SeedSafetyError("APP_ENVIRONMENT is not shadow")
    configured_run_id = _required(env, "LOAD_TEST_RUN_ID")
    if not RUN_ID_RE.fullmatch(requested_run_id):
        raise SeedSafetyError("run id must use KSHADOW format")
    if configured_run_id != requested_run_id:
        raise SeedSafetyError("run id does not match LOAD_TEST_RUN_ID")
    host = _required(env, "MYSQL_HOST")
    if host.lower() != "derived-mysql":
        raise SeedSafetyError("MYSQL_HOST must be derived-mysql")
    try:
        port = int(_required(env, "MYSQL_PORT"))
    except ValueError as exc:
        raise SeedSafetyError("MYSQL_PORT is invalid") from exc
    if not 1 <= port <= 65535:
        raise SeedSafetyError("MYSQL_PORT is invalid")

    databases = tuple(
        _required(env, name)
        for name in (
            "MYSQL_ONLINE_DATA_DB",
            "MYSQL_ARCHIVE_DB",
            "MYSQL_DAILY_REPORT_DB",
        )
    )
    if any(DATABASE_RE.fullmatch(database) is None for database in databases):
        raise SeedSafetyError("business shadow databases must use KShadow names")
    if len(set(databases)) != len(databases):
        raise SeedSafetyError("business shadow databases must be distinct")
    return ShadowContext(
        run_id=requested_run_id,
        host=host,
        port=port,
        user=_required(env, "MYSQL_USER"),
        password=_required(env, "MYSQL_PASSWORD"),
        online_db=databases[0],
        archive_db=databases[1],
        daily_db=databases[2],
    )


def validate_marker_rows(
    context: ShadowContext,
    rows_by_database: Mapping[str, Sequence[Sequence[Any]]],
) -> None:
    """Require exactly one identity row matching each configured database."""
    if set(rows_by_database) != set(context.databases):
        raise SeedSafetyError("shadow identity coverage is incomplete")
    for database in context.databases:
        rows = list(rows_by_database.get(database, ()))
        if len(rows) != 1:
            raise SeedSafetyError(f"shadow identity marker count mismatch: {database}")
        actual = tuple(str(value or "") for value in rows[0])
        expected = ("shadow", context.run_id, database)
        if actual != expected:
            raise SeedSafetyError(f"shadow identity marker mismatch: {database}")


async def verify_markers(context: ShadowContext) -> None:
    """Read and verify all three identity markers before ``init_db`` or writes."""
    rows_by_database: dict[str, Sequence[Sequence[Any]]] = {}
    for database in context.databases:
        connection = await aiomysql.connect(
            host=context.host,
            port=context.port,
            user=context.user,
            password=context.password,
            db=database,
            connect_timeout=5,
            autocommit=True,
            charset="utf8mb4",
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT environment,run_id,database_name "
                    "FROM _shadow_identity"
                )
                rows_by_database[database] = await cursor.fetchall()
        finally:
            connection.close()
    validate_marker_rows(context, rows_by_database)


def batches(items: Iterable[Any], size: int = MAX_BATCH_SIZE) -> Iterable[list[Any]]:
    """Yield bounded batches without loading another copy of the fixture."""
    if not 1 <= size <= MAX_BATCH_SIZE:
        raise ValueError("seed batch size must be between 1 and 100")
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def ensure_run_unused(
    existing_runs: Sequence[Sequence[Any]],
    run_id: str,
) -> None:
    """Reject a previously seeded run instead of clearing or resetting rows."""
    if any(str(row[0] or "") == run_id for row in existing_runs if row):
        raise SeedSafetyError(f"run {run_id} already has fixture")


def _task_values(task: Mapping[str, object]) -> dict[str, str]:
    """Convert one shared fixture task to the registered parser columns."""
    parser_type = str(task["parser_type"])
    parser = get_parser(parser_type)
    values = {column: "" for column in parser.COLUMNS}
    name = str(task["person_name"])
    identity = str(task["identity_number"])
    phone = str(task["phone"])
    address = str(task["original_address"])
    community = str(task["community"])
    inspector = str(task["assigned_user"])
    state = str(task["state"])
    ordinal = int(task["ordinal"])

    for field in ("姓名", "参考姓名"):
        if field in values:
            values[field] = name
    for field in ("身份证号", "身份证号码", "参考身份证号码"):
        if field in values:
            values[field] = identity
    for field in ("电话号码", "手机号码", "联系号码"):
        if field in values:
            values[field] = phone
    for field in ("地址", "地址1", "房屋地址", "高频抓拍小区", "疑似现住址"):
        if field in values:
            values[field] = address
    if "社区" in values:
        values["社区"] = community
    if "核查人" in values:
        values["核查人"] = inspector if state != "unassigned" else ""
    if "来源" in values:
        values["来源"] = "压测虚构任务"
    if "接警编号" in values:
        values["接警编号"] = f"LT-{ordinal:08d}"
    for field in ("下发日期", "下发时间", "创建时间"):
        if field in values:
            values[field] = "2026-09-02"
    for field in ("截止日期", "截止时间"):
        if field in values:
            values[field] = "2026-09-30"

    result_field = TASK_WORKFLOWS[parser_type].result_field
    if state == "pending_registration":
        values[result_field] = "待登记"
    elif state == "unverifiable":
        values[result_field] = "无法核实"
    elif state == "completed":
        values[result_field] = "离苏"
    return values


async def _read_source_revision(cursor, source_id: int) -> int:
    await cursor.execute(
        "SELECT revision FROM _online_source_rows WHERE id=%s",
        (source_id,),
    )
    row = await cursor.fetchone()
    if not row:
        raise SeedSafetyError("created source row disappeared")
    return int(row[0] or 1)


async def _ensure_expectation_schema(cursor) -> None:
    await cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {EXPECTATION_TABLE} (
            run_id VARCHAR(80) NOT NULL,
            ordinal_no INT NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            source_id BIGINT NOT NULL,
            initial_revision BIGINT UNSIGNED NOT NULL,
            scenario VARCHAR(30) NOT NULL,
            property_id BIGINT DEFAULT NULL,
            property_version INT UNSIGNED DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, ordinal_no),
            UNIQUE KEY uk_shadow_business_expectation_source (run_id, source_id),
            INDEX idx_shadow_business_expectation_scenario (run_id, scenario)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


async def _prepare_expectations(pool, run_id: str) -> None:
    async with pool.acquire() as connection:
        await connection.begin()
        try:
            async with connection.cursor() as cursor:
                await _ensure_expectation_schema(cursor)
                await cursor.execute(
                    f"SELECT run_id,COUNT(*) FROM {EXPECTATION_TABLE} "
                    "GROUP BY run_id"
                )
                ensure_run_unused(await cursor.fetchall(), run_id)
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise


async def _seed(run_id: str) -> dict[str, object]:
    context = validate_context(run_id)
    await verify_markers(context)
    await init_db()
    users = make_users()
    tasks = make_tasks()
    online_pool = db_manager.get_pool("online_data")
    registry_pool = db_manager.get_pool("registry")
    await _prepare_expectations(online_pool, context.run_id)

    async with online_pool.acquire() as connection:
        await connection.begin()
        try:
            async with connection.cursor() as cursor:
                for index in range(1, 13):
                    community = f"压测社区{index:02d}"
                    await cursor.execute(
                        "INSERT INTO _communities (name,is_active) VALUES (%s,1) "
                        "ON DUPLICATE KEY UPDATE is_active=1",
                        (community,),
                    )
                await cursor.execute(
                    "SELECT id,name FROM _communities WHERE name LIKE '压测社区%'"
                )
                community_ids = {
                    str(name): int(community_id)
                    for community_id, name in await cursor.fetchall()
                }
                for name, community_id in community_ids.items():
                    await cursor.execute(
                        "INSERT INTO _departments "
                        "(name,department_type,community_id,is_active) "
                        "VALUES (%s,'community',%s,1) ON DUPLICATE KEY UPDATE "
                        "department_type='community',community_id=VALUES(community_id),"
                        "is_active=1",
                        (name, community_id),
                    )
                await cursor.execute(
                    "SELECT id,community_id FROM _departments "
                    "WHERE name LIKE '压测社区%'"
                )
                department_ids = {
                    int(community_id): int(department_id)
                    for department_id, community_id in await cursor.fetchall()
                }
                await cursor.execute("SELECT id,code FROM _permission_groups")
                permission_groups = {
                    str(code): int(group_id)
                    for group_id, code in await cursor.fetchall()
                }
                role_group = {
                    "member": "flow_post",
                    "leader": "flow_post",
                    "internal_business": "internal_business",
                    "admin": "admin",
                    "super_admin": "super_admin",
                }
                for item in users:
                    username = str(item["username"])
                    display_name = str(item["display_name"])
                    logical_role = str(item["role"])
                    member_id = None
                    community_index = int(item["community_index"])
                    if community_index >= 0:
                        community_name = f"压测社区{community_index + 1:02d}"
                        department_id = department_ids[community_ids[community_name]]
                        await cursor.execute(
                            "INSERT INTO _grid_members "
                            "(name,community,department_id,position,status) "
                            "VALUES (%s,%s,%s,%s,'在岗') ON DUPLICATE KEY UPDATE "
                            "community=VALUES(community),department_id=VALUES(department_id),"
                            "position=VALUES(position),status='在岗'",
                            (display_name, community_name, department_id, str(item["position"])),
                        )
                        await cursor.execute(
                            "SELECT id FROM _grid_members WHERE name=%s",
                            (display_name,),
                        )
                        member_id = int((await cursor.fetchone())[0])
                        await cursor.execute(
                            "INSERT IGNORE INTO _grid_member_department_links "
                            "(member_id,department_id,sort_order) VALUES (%s,%s,0)",
                            (member_id, department_id),
                        )
                    database_role = (
                        logical_role
                        if logical_role in {"admin", "super_admin", "leader", "member"}
                        else "member"
                    )
                    group_id = permission_groups[role_group[logical_role]]
                    password_hash = bcrypt.hashpw(
                        password_hint(username).encode(), bcrypt.gensalt(rounds=10)
                    ).decode()
                    await cursor.execute(
                        "INSERT INTO _users "
                        "(username,display_name,password_hash,role,member_id,"
                        "permission_group_id,group_assignment_mode,password_is_temporary) "
                        "VALUES (%s,%s,%s,%s,%s,%s,'custom',0) "
                        "ON DUPLICATE KEY UPDATE display_name=VALUES(display_name),"
                        "password_hash=VALUES(password_hash),role=VALUES(role),"
                        "member_id=VALUES(member_id),permission_group_id=VALUES(permission_group_id),"
                        "group_assignment_mode='custom',password_is_temporary=0",
                        (
                            username,
                            display_name,
                            password_hash,
                            database_role,
                            member_id,
                            group_id,
                        ),
                    )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

    property_ids: dict[int, tuple[int, int]] = {}
    async with registry_pool.acquire() as connection:
        await connection.begin()
        try:
            async with connection.cursor() as cursor:
                for property_index in range(1, 49):
                    community_index = (property_index - 1) // 4 + 1
                    community_name = f"压测社区{community_index:02d}"
                    community_id = community_ids[community_name]
                    address = f"压测小区{property_index:02d}压测楼01幢01室"
                    normalized = f"压测小区{property_index:02d}压测楼1幢1室"
                    source_ref = f"shadow_loadtest:{context.run_id}:property:{property_index:02d}"
                    await cursor.execute(
                        "SELECT id,current_version FROM registry_properties "
                        "WHERE source_type='shadow_loadtest' AND source_ref=%s",
                        (source_ref,),
                    )
                    row = await cursor.fetchone()
                    if row:
                        property_id, version = int(row[0]), int(row[1])
                    else:
                        await cursor.execute(
                            "INSERT INTO registry_properties "
                            "(street,community_id,community_name_snapshot,natural_address,"
                            "building,room,normalized_address,status,current_version,"
                            "source_type,source_ref) VALUES ('压测街道',%s,%s,%s,"
                            "'压测楼01幢','01室',%s,'active',1,'shadow_loadtest',%s)",
                            (community_id, community_name, address, normalized, source_ref),
                        )
                        property_id, version = int(cursor.lastrowid), 1
                    property_ids[property_index] = (property_id, version)
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

    async with online_pool.acquire() as connection:
        await connection.begin()
        try:
            async with connection.cursor() as cursor:
                for property_index in range(1, 49):
                    community_index = (property_index - 1) // 4 + 1
                    community_name = f"压测社区{community_index:02d}"
                    community_id = community_ids[community_name]
                    small_name = f"压测小区{property_index:02d}"
                    await cursor.execute(
                        "INSERT INTO _police_address_entries "
                        "(name,normalized_name,detail_address,address_type,pattern,"
                        "community_id,aliases_json,source_flags,enabled) "
                        "VALUES (%s,%s,%s,'community','',%s,'[]',"
                        "'[\"shadow_loadtest\"]',1) ON DUPLICATE KEY UPDATE "
                        "detail_address=VALUES(detail_address),enabled=1",
                        (small_name, small_name, small_name, community_id),
                    )
            await connection.commit()
        except Exception:
            await connection.rollback()
            raise

    seeded_by_parser: dict[str, int] = {}
    for parser_type in TASK_WORKFLOWS:
        parser_tasks = [task for task in tasks if task["parser_type"] == parser_type]
        if not parser_tasks:
            continue
        seeded = 0
        for batch in batches(parser_tasks):
            async with online_pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor() as cursor:
                        expectation_rows = []
                        for task in batch:
                            values = _task_values(task)
                            source = await create_local_source_row(
                                cursor,
                                parser_type,
                                values,
                                source_kind="local_table",
                                source_ref="",
                            )
                            await enqueue_task_event(
                                cursor,
                                settings=settings,
                                domain="online",
                                event_type="online.task.created",
                                aggregate_type="online_task",
                                aggregate_id=f"{parser_type}:{source['row_key']}",
                                aggregate_revision=1,
                                audiences=["authenticated"],
                                task_id=f"{get_parser(parser_type).table_name}:{source['local_task_id']}",
                                source_id=int(source["id"]),
                                revision=await _read_source_revision(cursor, int(source["id"])),
                                operation_id=str(uuid.uuid4()),
                                changed_fields=list(values),
                            )
                            property_id = property_version = None
                            if str(task["state"]) == "pending_registration":
                                property_id, property_version = property_ids[
                                    int(task["property_index"])
                                ]
                                identity_hmac = hashlib.sha256(
                                    f"shadow:{context.run_id}:{task['identity_number']}".encode()
                                ).hexdigest()
                                await select_registration_property(
                                    cursor,
                                    parser_type=parser_type,
                                    row_key=str(source["row_key"]),
                                    source_id=int(source["id"]),
                                    property_id=property_id,
                                    property_version=property_version,
                                    source_revision=await _read_source_revision(
                                        cursor, int(source["id"])
                                    ),
                                    source_row_hash=str(source["row_hash"]),
                                    identity_hmac=identity_hmac,
                                    task_community=str(task["community"]),
                                    user_id=None,
                                )
                            scenario = (
                                "conflict"
                                if bool(task["conflict_group"])
                                else str(task["state"])
                            )
                            expectation_rows.append(
                                (
                                    context.run_id,
                                    int(task["ordinal"]),
                                    parser_type,
                                    str(source["row_key"]),
                                    int(source["id"]),
                                    await _read_source_revision(cursor, int(source["id"])),
                                    scenario,
                                    property_id,
                                    property_version,
                                )
                            )
                        await cursor.executemany(
                            f"INSERT INTO {EXPECTATION_TABLE} "
                            "(run_id,ordinal_no,parser_type,row_key,source_id,"
                            "initial_revision,scenario,property_id,property_version) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            expectation_rows,
                        )
                    await connection.commit()
                except Exception:
                    await connection.rollback()
                    raise
            seeded += len(batch)
        async with online_pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor() as cursor:
                    await rebuild_projection(cursor, parser_type)
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        seeded_by_parser[parser_type] = seeded

    return {
        "run_id": context.run_id,
        "users": len(users),
        "tasks": len(tasks),
        "properties": len(property_ids),
        "by_parser": seeded_by_parser,
    }


async def _run(run_id: str) -> tuple[int, dict[str, object] | None]:
    try:
        result = await _seed(run_id)
        return 0, result
    except SeedSafetyError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2, None
    except Exception as exc:  # noqa: BLE001 - do not print credentials or SQL
        print(
            json.dumps({"status": "failed", "error": type(exc).__name__}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1, None
    finally:
        await close_db()


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the isolated business Kafka shadow")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    code, result = asyncio.run(_run(args.run_id))
    if result is not None:
        print(json.dumps({"status": "seeded", **result}, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
