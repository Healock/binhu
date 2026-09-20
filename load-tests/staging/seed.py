"""Seed isolated Staging load fixtures through the application's own data layer.

This program runs only inside the Staging backend container.  The common
temporary password is read from stdin, hashed, and discarded.  The emitted
runtime index contains identifiers for fictional rows but no credential or
business source values.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import bcrypt

sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import DB_NAMES, close_db, db_manager, init_db  # noqa: E402
from fixture_model import make_accounts, make_tasks, runtime_index  # noqa: E402
from services.local_source import create_local_source_row  # noqa: E402
from services.online_source import rebuild_projection  # noqa: E402
from services.parsers import get_parser  # noqa: E402
from services.task_workflow import TASK_WORKFLOWS  # noqa: E402


RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")


def _guard(run_id: str) -> None:
    if not RUN_RE.fullmatch(run_id):
        raise RuntimeError("invalid Staging load run id")
    if os.environ.get("APP_ENVIRONMENT", "").strip().lower() != "staging":
        raise RuntimeError("APP_ENVIRONMENT is not staging")
    if os.environ.get("STAGING_LOAD_TEST_RUN_ID", "").strip().upper() != run_id:
        raise RuntimeError("STAGING_LOAD_TEST_RUN_ID does not match")
    if os.environ.get("MYSQL_HOST", "").strip().lower() != "environment-mysql":
        raise RuntimeError("seeder must use the isolated Staging MySQL service")
    expected_suffix = {
        "online_data": "OnlineData", "archive": "OnlineDataArchive",
        "daily_report": "daily_report", "platform": "PlatformData",
        "visit": "VisitData", "dispatch": "DispatchData",
        "registry": "RegistryData", "workflow": "WorkflowData",
    }
    for key, suffix in expected_suffix.items():
        value = str(DB_NAMES.get(key) or "")
        if not value.startswith("Staging_") or not value.endswith(suffix):
            raise RuntimeError("Staging domain database identity mismatch")


async def _verify_database_identities() -> None:
    for pool_name in (
        "online_data", "archive", "daily_report", "platform",
        "visit", "dispatch", "registry", "workflow",
    ):
        pool = db_manager.get_pool(pool_name)
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT id,environment FROM _environment_identity WHERE id=1")
                row = await cursor.fetchone()
                if row != (1, "staging"):
                    raise RuntimeError("Staging database marker mismatch")


def _task_values(task: dict[str, Any]) -> tuple[dict[str, str], str]:
    parser_type = str(task["parser_type"])
    parser = get_parser(parser_type)
    values = {column: "" for column in parser.COLUMNS}
    mappings = {
        ("姓名", "参考姓名"): str(task["person_name"]),
        ("身份证号", "身份证号码", "参考身份证号码"): str(task["identity_number"]),
        ("电话号码", "手机号码", "联系号码"): str(task["phone"]),
        ("地址", "地址1", "房屋地址", "高频抓拍小区", "疑似现住址", "现住址"):
            str(task["original_address"]),
    }
    for fields, value in mappings.items():
        for field in fields:
            if field in values:
                values[field] = value
    if "社区" in values:
        values["社区"] = str(task["community"])
    if "核查人" in values:
        values["核查人"] = str(task["assigned_user"])
    if "来源" in values:
        values["来源"] = "Staging 预发布虚构任务"
    if "接警编号" in values:
        values["接警编号"] = f"STG-LT-{int(task['ordinal']):08d}"
    for field in ("下发日期", "下发时间", "创建时间"):
        if field in values:
            values[field] = "2026-09-20"
    for field in ("截止日期", "截止时间"):
        if field in values:
            values[field] = "2026-10-20"
    editable = next((field for field in ("现住址", "地址", "备注", "核查结果") if field in values), "")
    if not editable:
        raise RuntimeError(f"{parser_type} has no controlled editable load-test field")
    return values, editable


async def _seed(run_id: str, password: str) -> dict[str, Any]:
    _guard(run_id)
    if len(password) < 16 or len(password) > 256:
        raise RuntimeError("private Staging fixture password is invalid")
    await init_db()
    await _verify_database_identities()
    accounts = make_accounts()
    tasks = make_tasks()
    online_pool = db_manager.get_pool("online_data")

    async with online_pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "CREATE TABLE IF NOT EXISTS _staging_loadtest_expectations ("
                    "run_id VARCHAR(32) NOT NULL,ordinal_no INT NOT NULL,"
                    "parser_type VARCHAR(50) NOT NULL,row_key CHAR(32) NOT NULL,"
                    "source_id BIGINT NOT NULL,scenario VARCHAR(30) NOT NULL,"
                    "assigned_username VARCHAR(191) NOT NULL DEFAULT '',"
                    "PRIMARY KEY(run_id,ordinal_no),"
                    "UNIQUE KEY uk_staging_load_source(run_id,source_id),"
                    "INDEX idx_staging_load_scenario(run_id,scenario)"
                    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                )
                for index in range(1, 13):
                    name = f"预发布演练社区{index:02d}"
                    await cur.execute(
                        "INSERT INTO _communities(name,is_active) VALUES(%s,1) "
                        "ON DUPLICATE KEY UPDATE is_active=1", (name,),
                    )
                await cur.execute("SELECT id,name FROM _communities WHERE name LIKE '预发布演练社区%'")
                communities = {str(name): int(cid) for cid, name in await cur.fetchall()}
                for name, community_id in communities.items():
                    await cur.execute(
                        "INSERT INTO _departments(name,department_type,community_id,is_active) "
                        "VALUES(%s,'community',%s,1) ON DUPLICATE KEY UPDATE "
                        "department_type='community',community_id=VALUES(community_id),is_active=1",
                        (name, community_id),
                    )
                await cur.execute(
                    "SELECT id,community_id FROM _departments WHERE name LIKE '预发布演练社区%'"
                )
                departments = {int(cid): int(did) for did, cid in await cur.fetchall()}
                await cur.execute("SELECT id,code FROM _permission_groups")
                permission_groups = {str(code): int(gid) for gid, code in await cur.fetchall()}
                role_groups = {
                    "member": "flow_post", "leader": "flow_post",
                    "internal_business": "internal_business", "admin": "admin",
                    "super_admin": "super_admin",
                }
                password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()
                for item in accounts:
                    community = f"预发布演练社区{int(item['community_index']) + 1:02d}"
                    community_id = communities[community]
                    department_id = departments[community_id]
                    await cur.execute(
                        "INSERT INTO _grid_members(name,community,department_id,position,status) "
                        "VALUES(%s,%s,%s,%s,'在岗') ON DUPLICATE KEY UPDATE "
                        "community=VALUES(community),department_id=VALUES(department_id),"
                        "position=VALUES(position),status='在岗'",
                        (item["display_name"], community, department_id, item["position"]),
                    )
                    await cur.execute("SELECT id FROM _grid_members WHERE name=%s", (item["display_name"],))
                    member_id = int((await cur.fetchone())[0])
                    await cur.execute(
                        "INSERT IGNORE INTO _grid_member_department_links(member_id,department_id,sort_order) "
                        "VALUES(%s,%s,0)", (member_id, department_id),
                    )
                    database_role = item["role"] if item["role"] in {"admin", "super_admin", "leader", "member"} else "member"
                    group_id = permission_groups[role_groups[str(item["role"])]]
                    await cur.execute(
                        "INSERT INTO _users(username,display_name,password_hash,role,member_id,"
                        "permission_group_id,group_assignment_mode,password_is_temporary) "
                        "VALUES(%s,%s,%s,%s,%s,%s,'custom',0) ON DUPLICATE KEY UPDATE "
                        "display_name=VALUES(display_name),password_hash=VALUES(password_hash),"
                        "role=VALUES(role),member_id=VALUES(member_id),"
                        "permission_group_id=VALUES(permission_group_id),group_assignment_mode='custom',"
                        "password_is_temporary=0",
                        (item["username"], item["display_name"], password_hash,
                         database_role, member_id, group_id),
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    seeded: list[dict[str, Any]] = []
    for parser_type in TASK_WORKFLOWS:
        parser_tasks = [item for item in tasks if item["parser_type"] == parser_type]
        for start in range(0, len(parser_tasks), 100):
            batch = parser_tasks[start:start + 100]
            async with online_pool.acquire() as conn:
                await conn.begin()
                try:
                    async with conn.cursor() as cur:
                        expectations = []
                        for task in batch:
                            values, editable = _task_values(task)
                            source = await create_local_source_row(
                                cur, parser_type, values, source_kind="local_table",
                                source_ref=f"staging:{run_id}:task:{int(task['ordinal']):05d}",
                            )
                            expectations.append((
                                run_id, int(task["ordinal"]), parser_type,
                                str(source["row_key"]), int(source["id"]),
                                str(task["scenario"]), str(task["assigned_username"]),
                            ))
                            seeded.append({
                                "parser_type": parser_type,
                                "row_key": str(source["row_key"]),
                                "source_id": int(source["id"]),
                                "scenario": str(task["scenario"]),
                                "assigned_username": str(task["assigned_username"]),
                                "claim_username": str(task["claim_username"]),
                                "assigner_username": str(task["assigner_username"]),
                                "community": str(task["community"]),
                                "assignment_inspector": str(task["assignment_inspector"]),
                                "editable_field": editable,
                            })
                        await cur.executemany(
                            "INSERT INTO _staging_loadtest_expectations "
                            "(run_id,ordinal_no,parser_type,row_key,source_id,scenario,assigned_username) "
                            "VALUES(%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                            "parser_type=VALUES(parser_type),row_key=VALUES(row_key),"
                            "source_id=VALUES(source_id),scenario=VALUES(scenario),"
                            "assigned_username=VALUES(assigned_username)", expectations,
                        )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        async with online_pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await rebuild_projection(cur, parser_type)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    return runtime_index(run_id, seeded)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--password-stdin", action="store_true", required=True)
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n")
    try:
        result = await _seed(args.run_id.strip().upper(), password)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        password = ""
        await close_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
