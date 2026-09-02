"""Seed the isolated shadow database through the application's own source layer.

This file is mounted read-only into the exact backend image and executed there.
It refuses to run unless the container identifies itself as the requested shadow
run. It never accepts a production hostname or an unsafe override.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import bcrypt

sys.path.insert(0, "/app")
sys.path.insert(0, "/load-tests")

from database import close_db, db_manager, init_db  # noqa: E402
from fixture import make_tasks, make_users, password_hint  # noqa: E402
from services.local_source import create_local_source_row  # noqa: E402
from services.online_source import rebuild_projection  # noqa: E402
from services.parsers import get_parser  # noqa: E402
from services.task_registration import select_registration_property  # noqa: E402
from services.task_workflow import TASK_WORKFLOWS  # noqa: E402


def _guard(run_id: str) -> None:
    if os.environ.get("APP_ENVIRONMENT", "").strip().lower() != "shadow":
        raise RuntimeError("APP_ENVIRONMENT is not shadow")
    if os.environ.get("LOAD_TEST_RUN_ID", "").strip().upper() != run_id.upper():
        raise RuntimeError("LOAD_TEST_RUN_ID does not match")
    database = os.environ.get("MYSQL_ONLINE_DATA_DB", "")
    if not database.startswith("LoadTest_"):
        raise RuntimeError("shadow database name must start with LoadTest_")
    if os.environ.get("MYSQL_HOST", "").strip().lower() != "mysql":
        raise RuntimeError("seeder must run inside the shadow backend network")


def _task_values(task: dict[str, object]) -> dict[str, str]:
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


async def _seed(run_id: str) -> dict[str, object]:
    _guard(run_id)
    await init_db()
    users = make_users()
    tasks = make_tasks()
    online_pool = db_manager.get_pool("online_data")
    registry_pool = db_manager.get_pool("registry")

    async with online_pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute("SHOW TABLES LIKE '_shadow_loadtest_marker'")
                if not await cur.fetchone():
                    raise RuntimeError("shadow marker table is missing")
                await cur.execute(
                    "SELECT COUNT(*) FROM _shadow_loadtest_marker WHERE environment='shadow'"
                )
                if int((await cur.fetchone())[0] or 0) < 1:
                    raise RuntimeError("database does not carry the shadow marker")
                await cur.execute(
                    "DELETE FROM _shadow_loadtest_marker WHERE run_id='__UNSEEDED__'"
                )
                await cur.execute(
                    "INSERT INTO _shadow_loadtest_marker (run_id,environment) VALUES (%s,'shadow') "
                    "ON DUPLICATE KEY UPDATE environment='shadow'",
                    (run_id,),
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS _shadow_loadtest_expectations (
                        run_id VARCHAR(32) NOT NULL,
                        ordinal_no INT NOT NULL,
                        parser_type VARCHAR(50) NOT NULL,
                        row_key CHAR(32) NOT NULL,
                        source_id BIGINT NOT NULL,
                        initial_revision BIGINT UNSIGNED NOT NULL,
                        scenario VARCHAR(30) NOT NULL,
                        property_id BIGINT DEFAULT NULL,
                        property_version INT UNSIGNED DEFAULT NULL,
                        PRIMARY KEY (run_id,ordinal_no),
                        UNIQUE KEY uk_shadow_expectation_source (run_id,source_id),
                        INDEX idx_shadow_expectation_scenario (run_id,scenario)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                for index in range(1, 13):
                    community = f"压测社区{index:02d}"
                    await cur.execute(
                        "INSERT INTO _communities (name,is_active) VALUES (%s,1) "
                        "ON DUPLICATE KEY UPDATE is_active=1",
                        (community,),
                    )
                await cur.execute(
                    "SELECT id,name FROM _communities WHERE name LIKE '压测社区%'"
                )
                community_ids = {str(name): int(cid) for cid, name in await cur.fetchall()}
                for name, community_id in community_ids.items():
                    await cur.execute(
                        "INSERT INTO _departments (name,department_type,community_id,is_active) "
                        "VALUES (%s,'community',%s,1) ON DUPLICATE KEY UPDATE "
                        "department_type='community',community_id=VALUES(community_id),is_active=1",
                        (name, community_id),
                    )
                await cur.execute(
                    "SELECT id,community_id FROM _departments WHERE name LIKE '压测社区%'"
                )
                department_ids = {int(cid): int(did) for did, cid in await cur.fetchall()}
                await cur.execute("SELECT id,code FROM _permission_groups")
                permission_groups = {str(code): int(gid) for gid, code in await cur.fetchall()}

                role_group = {
                    "member": "flow_post", "leader": "flow_post",
                    "internal_business": "internal_business", "admin": "admin",
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
                        await cur.execute(
                            "INSERT INTO _grid_members (name,community,department_id,position,status) "
                            "VALUES (%s,%s,%s,%s,'在岗') ON DUPLICATE KEY UPDATE "
                            "community=VALUES(community),department_id=VALUES(department_id),"
                            "position=VALUES(position),status='在岗'",
                            (display_name, community_name, department_id, str(item["position"])),
                        )
                        await cur.execute("SELECT id FROM _grid_members WHERE name=%s", (display_name,))
                        member_id = int((await cur.fetchone())[0])
                        await cur.execute(
                            "INSERT IGNORE INTO _grid_member_department_links "
                            "(member_id,department_id,sort_order) VALUES (%s,%s,0)",
                            (member_id, department_id),
                        )
                    database_role = logical_role if logical_role in {"admin", "super_admin", "leader", "member"} else "member"
                    group_id = permission_groups[role_group[logical_role]]
                    password_hash = bcrypt.hashpw(
                        password_hint(username).encode(), bcrypt.gensalt(rounds=10)
                    ).decode()
                    await cur.execute(
                        "INSERT INTO _users (username,display_name,password_hash,role,member_id,"
                        "permission_group_id,group_assignment_mode,password_is_temporary) "
                        "VALUES (%s,%s,%s,%s,%s,%s,'custom',0) ON DUPLICATE KEY UPDATE "
                        "display_name=VALUES(display_name),password_hash=VALUES(password_hash),"
                        "role=VALUES(role),member_id=VALUES(member_id),"
                        "permission_group_id=VALUES(permission_group_id),group_assignment_mode='custom',"
                        "password_is_temporary=0",
                        (username, display_name, password_hash, database_role, member_id, group_id),
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    property_ids: dict[int, tuple[int, int]] = {}
    async with registry_pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                for property_index in range(1, 49):
                    community_index = (property_index - 1) // 4 + 1
                    community_name = f"压测社区{community_index:02d}"
                    community_id = community_ids[community_name]
                    address = f"压测小区{property_index:02d}压测楼01幢01室"
                    normalized = f"压测小区{property_index:02d}压测楼1幢1室"
                    source_ref = f"shadow:{run_id}:property:{property_index:02d}"
                    await cur.execute(
                        "SELECT id,current_version FROM registry_properties WHERE source_type='shadow_loadtest' AND source_ref=%s",
                        (source_ref,),
                    )
                    row = await cur.fetchone()
                    if row:
                        property_id, version = int(row[0]), int(row[1])
                    else:
                        await cur.execute(
                            "INSERT INTO registry_properties (street,community_id,community_name_snapshot,"
                            "natural_address,building,room,normalized_address,status,current_version,"
                            "source_type,source_ref) VALUES ('压测街道',%s,%s,%s,'压测楼01幢','01室',"
                            "%s,'active',1,'shadow_loadtest',%s)",
                            (community_id, community_name, address, normalized, source_ref),
                        )
                        property_id, version = int(cur.lastrowid), 1
                    property_ids[property_index] = (property_id, version)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    seeded_by_parser: dict[str, int] = {}
    async with online_pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                for property_index in range(1, 49):
                    community_index = (property_index - 1) // 4 + 1
                    community_name = f"压测社区{community_index:02d}"
                    community_id = community_ids[community_name]
                    small_name = f"压测小区{property_index:02d}"
                    await cur.execute(
                        "INSERT INTO _police_address_entries (name,normalized_name,detail_address,"
                        "address_type,pattern,community_id,aliases_json,source_flags,enabled) "
                        "VALUES (%s,%s,%s,'community','',%s,'[]','[\"shadow_loadtest\"]',1) "
                        "ON DUPLICATE KEY UPDATE detail_address=VALUES(detail_address),enabled=1",
                        (small_name, small_name, small_name, community_id),
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    # Seed in bounded batches. A failed run can be safely re-executed before
    # load generation because create_local_source_row and the expectation key
    # are idempotent; a database changed by a previous test is rejected as a
    # content conflict instead of being silently reset.
    for parser_type in TASK_WORKFLOWS:
        parser_tasks = [task for task in tasks if task["parser_type"] == parser_type]
        if not parser_tasks:
            continue
        for start in range(0, len(parser_tasks), 100):
            batch = parser_tasks[start:start + 100]
            async with online_pool.acquire() as conn:
                await conn.begin()
                try:
                    async with conn.cursor() as cur:
                        expectation_rows = []
                        for task in batch:
                            values = _task_values(task)
                            source = await create_local_source_row(
                                cur, parser_type, values,
                                # Exercise the same active local-source path as
                                # production. The run-scoped source_ref keeps
                                # shadow fixtures identifiable without adding
                                # a shadow-only source kind to business code.
                                source_kind="local_table",
                                source_ref=(
                                    f"shadow:{run_id}:task:{int(task['ordinal']):04d}"
                                ),
                            )
                            property_id = property_version = None
                            if str(task["state"]) == "pending_registration":
                                property_id, property_version = property_ids[
                                    int(task["property_index"])
                                ]
                                identity_hmac = hashlib.sha256(
                                    f"shadow:{run_id}:{task['identity_number']}".encode()
                                ).hexdigest()
                                await select_registration_property(
                                    cur,
                                    parser_type=parser_type,
                                    row_key=str(source["row_key"]),
                                    source_id=int(source["id"]),
                                    property_id=property_id,
                                    property_version=property_version,
                                    source_revision=1,
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
                            expectation_rows.append((
                                run_id, int(task["ordinal"]), parser_type,
                                str(source["row_key"]), int(source["id"]), 1,
                                scenario, property_id, property_version,
                            ))
                        await cur.executemany(
                            "INSERT INTO _shadow_loadtest_expectations "
                            "(run_id,ordinal_no,parser_type,row_key,source_id,initial_revision,"
                            "scenario,property_id,property_version) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                            "ON DUPLICATE KEY UPDATE parser_type=VALUES(parser_type),"
                            "row_key=VALUES(row_key),source_id=VALUES(source_id),"
                            "initial_revision=VALUES(initial_revision),scenario=VALUES(scenario),"
                            "property_id=VALUES(property_id),property_version=VALUES(property_version)",
                            expectation_rows,
                        )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
            seeded_by_parser[parser_type] = (
                seeded_by_parser.get(parser_type, 0) + len(batch)
            )

        async with online_pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await rebuild_projection(cur, parser_type)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    return {
        "run_id": run_id,
        "users": len(users),
        "tasks": len(tasks),
        "properties": len(property_ids),
        "by_parser": seeded_by_parser,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        result = await _seed(args.run_id.strip().upper())
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        await close_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
