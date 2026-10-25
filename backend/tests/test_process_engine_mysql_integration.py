"""Opt-in integration coverage for PlatformData/WorkflowData routing.

Run only with dedicated database schemas whose names all end in ``_test``.
The suite creates and removes its fixture process rows, but leaves the schema
in place for repeatable runs.
"""

from __future__ import annotations

import os
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import settings
from database import DomainRoutingCursor
from services.domain_schema import ensure_workflow_schema
from services.process_engine import ProcessEngine, POSITION_ORDER_CONFIG
from services.process_definition_loader import load_process_bundle


ENABLED = os.getenv("RUN_PROCESS_ENGINE_MYSQL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="set RUN_PROCESS_ENGINE_MYSQL_INTEGRATION=1 and dedicated *_test database settings",
)


def _require_isolated_databases() -> None:
    if settings.APP_ENVIRONMENT == "production":
        pytest.fail("integration test refuses APP_ENVIRONMENT=production")
    databases = {
        "online": settings.MYSQL_ONLINE_DATA_DB,
        "platform": settings.MYSQL_PLATFORM_DB,
        "workflow": settings.MYSQL_WORKFLOW_DB,
    }
    if len(set(databases.values())) != len(databases):
        pytest.fail("integration requires three distinct test databases")
    if any(not name.lower().endswith("_test") for name in databases.values()):
        pytest.fail("integration refuses database names without the _test suffix")
    if not settings.PLATFORM_DOMAIN_ACTIVE:
        pytest.fail("integration requires PLATFORM_DOMAIN_ACTIVE=true to exercise split-domain routing")


async def _create_platform_fixtures(cur) -> tuple[int, int, int, int, int, int, int]:
    async def ensure_column(table: str, column: str, definition: str) -> None:
        await cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s",
            (table, column),
        )
        if await cur.fetchone() is None:
            await cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")

    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _users (
            id BIGINT PRIMARY KEY,
            member_id BIGINT DEFAULT NULL,
            username VARCHAR(200) NOT NULL DEFAULT '',
            display_name VARCHAR(100) NOT NULL DEFAULT '',
            role VARCHAR(40) NOT NULL DEFAULT 'user'
        ) ENGINE=InnoDB
    """)
    await ensure_column("_users", "username", "VARCHAR(200) NOT NULL DEFAULT ''")
    await ensure_column("_users", "display_name", "VARCHAR(100) NOT NULL DEFAULT ''")
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _grid_members (
            id BIGINT PRIMARY KEY,
            name VARCHAR(100) NOT NULL DEFAULT '',
            position VARCHAR(100) NOT NULL,
            status VARCHAR(20) NOT NULL
        ) ENGINE=InnoDB
    """)
    await ensure_column("_grid_members", "name", "VARCHAR(100) NOT NULL DEFAULT ''")
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _departments (
            id BIGINT PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            parent_id BIGINT DEFAULT NULL,
            department_type VARCHAR(30) NOT NULL DEFAULT 'unit',
            is_active TINYINT(1) NOT NULL DEFAULT 1
        ) ENGINE=InnoDB
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _grid_member_department_links (
            member_id BIGINT NOT NULL,
            department_id BIGINT NOT NULL,
            sort_order INT NOT NULL DEFAULT 0,
            PRIMARY KEY (member_id, department_id)
        ) ENGINE=InnoDB
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _notifications (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            category VARCHAR(30) NOT NULL,
            severity VARCHAR(20) NOT NULL,
            title VARCHAR(100) NOT NULL,
            content VARCHAR(1000) NOT NULL,
            related_task_id BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL
        ) ENGINE=InnoDB
    """)
    member_id = int(uuid.uuid4().int % 2_000_000_000) + 1
    peer_member_id = member_id + 1
    manager_member_id = member_id + 2
    user_id = member_id + 2_000_000_001
    peer_user_id = user_id + 1
    manager_user_id = user_id + 2
    department_id = member_id + 4_000_000_001
    await cur.execute(
        "INSERT INTO _grid_members (id,name,position,status) VALUES "
        "(%s,'测试发起人','组员','在岗'),(%s,'测试同事','组员','在岗'),(%s,'测试直属领导','组长','在岗')",
        (member_id, peer_member_id, manager_member_id),
    )
    await cur.execute(
        "INSERT INTO _users (id,member_id,username,display_name,role) VALUES "
        "(%s,%s,%s,'测试发起人','user'),(%s,%s,%s,'测试同事','user'),(%s,%s,%s,'测试直属领导','user'),(%s,NULL,%s,'测试管理员','admin')",
        (user_id, member_id, f"test_{user_id}",
         peer_user_id, peer_member_id, f"test_{peer_user_id}",
         manager_user_id, manager_member_id, f"test_{manager_user_id}",
         user_id + 3, f"test_{user_id + 3}"),
    )
    await cur.execute(
        "INSERT INTO _departments (id,name,parent_id,department_type,is_active) VALUES (%s,'测试社区',NULL,'community',1)",
        (department_id,),
    )
    await cur.execute(
        "INSERT INTO _grid_member_department_links (member_id,department_id) VALUES "
        "(%s,%s),(%s,%s),(%s,%s)",
        (member_id, department_id, peer_member_id, department_id,
         manager_member_id, department_id),
    )
    return (user_id, member_id, department_id, peer_user_id, manager_user_id,
            peer_member_id, manager_member_id)


@pytest.mark.asyncio
async def test_mysql_split_domains_advance_empty_position_order_to_admin_hold():
    _require_isolated_databases()
    import aiomysql

    pool_options = {
        "host": settings.MYSQL_HOST,
        "port": settings.MYSQL_PORT,
        "user": settings.MYSQL_USER,
        "password": settings.MYSQL_PASSWORD,
        "minsize": 1,
        "maxsize": 2,
        "charset": "utf8mb4",
        "autocommit": False,
        "cursorclass": DomainRoutingCursor,
    }
    online_pool = await aiomysql.create_pool(db=settings.MYSQL_ONLINE_DATA_DB, **pool_options)
    platform_pool = await aiomysql.create_pool(db=settings.MYSQL_PLATFORM_DB, **pool_options)
    workflow_pool = await aiomysql.create_pool(db=settings.MYSQL_WORKFLOW_DB, **pool_options)
    process_ids = []
    fixture_user_id = fixture_member_id = None
    peer_user_id = manager_user_id = None
    peer_member_id = manager_member_id = None
    prior_config = None
    config_existed = False
    try:
        async with platform_pool.acquire() as conn:
            async with conn.cursor(aiomysql.Cursor) as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _system_config (
                        config_key VARCHAR(100) PRIMARY KEY,
                        config_value TEXT
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                await cur.execute("SELECT config_value FROM _system_config WHERE config_key=%s",
                                  (POSITION_ORDER_CONFIG,))
                row = await cur.fetchone()
                config_existed = row is not None
                prior_config = row[0] if row else None
                await cur.execute(
                    "INSERT INTO _system_config (config_key,config_value) VALUES (%s,'[]') "
                    "ON DUPLICATE KEY UPDATE config_value='[]'", (POSITION_ORDER_CONFIG,),
                )
                (fixture_user_id, fixture_member_id, _, peer_user_id, manager_user_id,
                 peer_member_id, manager_member_id) = await _create_platform_fixtures(cur)
            await conn.commit()

        async with online_pool.acquire() as conn:
            async with conn.cursor(aiomysql.Cursor) as cur:
                await cur.execute("SHOW TABLES LIKE '_system_config'")
                if await cur.fetchone() is not None:
                    await cur.execute("SELECT config_value FROM _system_config WHERE config_key=%s",
                                      (POSITION_ORDER_CONFIG,))
                    assert await cur.fetchone() is None
            await conn.commit()

        async with workflow_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await ensure_workflow_schema(cur)
                await cur.execute("SHOW TABLES LIKE '_system_config'")
                assert await cur.fetchone() is None
            await conn.commit()

        now = datetime.utcnow().replace(microsecond=0)
        async with workflow_pool.acquire() as conn:
            await conn.begin()
            async with conn.cursor() as cur:
                engine = ProcessEngine(cur)
                process_id = await engine.create_process(
                    code=f"mysql_b1_empty_{uuid.uuid4().hex[:12]}", name="跨库空岗位顺序集成测试"
                )
                process_ids.append(process_id)
                version_id = await engine.publish_process(process_id, [{
                    "node_key": "approval",
                    "name": "审批",
                    "assignee_type": "指定成员",
                    "assignee_params": {"user_id": fixture_user_id},
                    "timeout_hours": 1,
                }])
                instance = await engine.start(
                    process_version_id=version_id,
                    requester_user_id=fixture_user_id,
                    title="跨库超时集成测试",
                    form_data={"synthetic": True},
                    now=now - timedelta(hours=4),
                )
                instance_id = instance["id"]
            await conn.commit()

        async with workflow_pool.acquire() as conn:
            await conn.begin()
            async with conn.cursor() as cur:
                assert await ProcessEngine(cur).advance_timeouts(now=now - timedelta(hours=2)) == 1
                await cur.execute("SELECT escalation_stage,status FROM process_tasks WHERE instance_id=%s",
                                  (instance_id,))
                assert await cur.fetchone() == (1, "pending")
            await conn.commit()

        async with workflow_pool.acquire() as conn:
            await conn.begin()
            async with conn.cursor() as cur:
                assert await ProcessEngine(cur).advance_timeouts(now=now) == 1
                await cur.execute("SELECT id,escalation_stage,status FROM process_tasks WHERE instance_id=%s",
                                  (instance_id,))
                held_task_id, held_stage, held_status = await cur.fetchone()
                assert (held_stage, held_status) == (1, "admin_hold")
                await cur.execute("SELECT status FROM process_instances WHERE id=%s", (instance_id,))
                assert (await cur.fetchone())[0] == "admin_hold"
            await conn.commit()

        async with workflow_pool.acquire() as conn:
            await conn.begin()
            async with conn.cursor() as cur:
                terminated = await ProcessEngine(cur).admin_intervene(
                    instance_id=instance_id, actor_user_id=fixture_user_id + 3,
                    terminate=True, note="集成测试管理员终止", now=now + timedelta(hours=1),
                )
                assert terminated["status"] == "terminated"
            await conn.commit()
        async with platform_pool.acquire() as conn:
            async with conn.cursor(aiomysql.Cursor) as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM _notifications WHERE user_id=%s AND category=%s",
                    (fixture_user_id, "workflow_admin_terminated"),
                )
                assert (await cur.fetchone())[0] == 1

        async with platform_pool.acquire() as conn:
            async with conn.cursor(aiomysql.Cursor) as cur:
                await cur.execute(
                    "UPDATE _system_config SET config_value=%s WHERE config_key=%s",
                    (json.dumps(["组长", "组员"], ensure_ascii=False), POSITION_ORDER_CONFIG),
                )
            await conn.commit()

        async with workflow_pool.acquire() as conn:
            await conn.begin()
            async with conn.cursor() as cur:
                engine = ProcessEngine(cur)
                process_id = await engine.create_process(
                    code=f"mysql_b1_nonempty_{uuid.uuid4().hex[:12]}", name="跨库岗位顺序集成测试"
                )
                process_ids.append(process_id)
                version_id = await engine.publish_process(process_id, [{
                    "node_key": "approval",
                    "name": "审批",
                    "assignee_type": "指定成员",
                    "assignee_params": {"user_id": fixture_user_id},
                    "timeout_hours": 1,
                }])
                instance = await engine.start(
                    process_version_id=version_id,
                    requester_user_id=fixture_user_id,
                    title="跨库岗位顺序集成测试",
                    form_data={"synthetic": True},
                    now=now - timedelta(hours=4),
                )
                position_instance_id = instance["id"]
            await conn.commit()

        async with workflow_pool.acquire() as conn:
            await conn.begin()
            async with conn.cursor() as cur:
                assert await ProcessEngine(cur).advance_timeouts(now=now - timedelta(hours=2)) == 1
                await cur.execute(
                    "SELECT escalation_stage,status,assignees FROM process_tasks WHERE instance_id=%s",
                    (position_instance_id,),
                )
                stage, status, assignees = await cur.fetchone()
                assert (stage, status, json.loads(assignees)) == (1, "pending", [fixture_user_id, peer_user_id])
            await conn.commit()

        async with workflow_pool.acquire() as conn:
            await conn.begin()
            async with conn.cursor() as cur:
                assert await ProcessEngine(cur).advance_timeouts(now=now) == 1
                await cur.execute(
                    "SELECT escalation_stage,status,assignees FROM process_tasks WHERE instance_id=%s",
                    (position_instance_id,),
                )
                stage, status, assignees = await cur.fetchone()
                assert (stage, status, json.loads(assignees)) == (
                    2, "pending", [fixture_user_id, peer_user_id, manager_user_id]
                )
            await conn.commit()

        async with workflow_pool.acquire() as conn:
            await conn.begin()
            async with conn.cursor() as cur:
                # The next timeout exhausts the configured sequence and must
                # hold the same task for an administrator.
                assert await ProcessEngine(cur).advance_timeouts(now=now + timedelta(hours=2)) == 1
                await cur.execute(
                    "SELECT id,escalation_stage,status FROM process_tasks WHERE instance_id=%s",
                    (position_instance_id,),
                )
                task_id, stage, status = await cur.fetchone()
                assert (stage, status) == (2, "admin_hold")
                await cur.execute("SELECT status FROM process_instances WHERE id=%s", (position_instance_id,))
                assert (await cur.fetchone())[0] == "admin_hold"
                released = await ProcessEngine(cur).admin_intervene(
                    instance_id=position_instance_id, actor_user_id=fixture_user_id + 3,
                    assignee_user_id=manager_user_id, note="集成测试管理员放行",
                    now=now + timedelta(hours=3),
                )
                assert released["status"] == "running"
                await cur.execute("SELECT status,assignees FROM process_tasks WHERE id=%s", (task_id,))
                task_status, released_assignees = await cur.fetchone()
                assert (task_status, json.loads(released_assignees)) == ("pending", [manager_user_id])
                completed = await ProcessEngine(cur).decide(
                    task_id=task_id, actor_user_id=manager_user_id, decision="complete",
                    note="集成测试完成", now=now + timedelta(hours=4),
                )
                assert completed["status"] == "completed"
                await cur.execute("SELECT status,completed_at FROM process_instances WHERE id=%s", (position_instance_id,))
                instance_status, completed_at = await cur.fetchone()
                assert instance_status == "completed"
                assert completed_at is not None
                await cur.execute(
                    "SELECT event_type FROM process_events WHERE instance_id=%s ORDER BY id",
                    (position_instance_id,),
                )
                event_types = [row[0] for row in await cur.fetchall()]
                assert "task_escalated" in event_types
                assert "admin_intervention_required" in event_types
                assert "admin_released" in event_types
                assert "task_decided" in event_types
            await conn.commit()
        async with platform_pool.acquire() as conn:
            async with conn.cursor(aiomysql.Cursor) as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM _notifications WHERE user_id=%s AND category=%s",
                    (manager_user_id, "workflow_admin_released"),
                )
                assert (await cur.fetchone())[0] == 1
    finally:
        for process_id in process_ids:
            async with workflow_pool.acquire() as conn:
                async with conn.cursor(aiomysql.Cursor) as cur:
                    await cur.execute("SELECT id FROM process_instances WHERE process_definition_id=%s",
                                      (process_id,))
                    instance_ids = [row[0] for row in await cur.fetchall()]
                    if instance_ids:
                        placeholders = ",".join(["%s"] * len(instance_ids))
                        params = tuple(instance_ids)
                        await cur.execute(f"DELETE FROM process_events WHERE instance_id IN ({placeholders})", params)
                        await cur.execute(f"DELETE FROM process_attachments WHERE instance_id IN ({placeholders})", params)
                        await cur.execute(f"DELETE FROM process_tasks WHERE instance_id IN ({placeholders})", params)
                        await cur.execute(f"DELETE FROM process_instances WHERE id IN ({placeholders})", params)
                    await cur.execute("DELETE FROM process_versions WHERE process_definition_id=%s", (process_id,))
                    await cur.execute("DELETE FROM process_definitions WHERE id=%s", (process_id,))
                await conn.commit()
        if fixture_user_id is not None:
            async with platform_pool.acquire() as conn:
                async with conn.cursor(aiomysql.Cursor) as cur:
                    user_ids = (fixture_user_id, peer_user_id, manager_user_id, fixture_user_id + 3)
                    member_ids = (fixture_member_id, peer_member_id, manager_member_id)
                    await cur.execute(
                        "DELETE FROM _notifications WHERE user_id IN (%s,%s,%s,%s)", user_ids
                    )
                    await cur.execute("DELETE FROM _users WHERE id IN (%s,%s,%s,%s)", user_ids)
                    await cur.execute(
                        "DELETE FROM _grid_member_department_links WHERE member_id IN (%s,%s,%s)",
                        member_ids,
                    )
                    await cur.execute("DELETE FROM _grid_members WHERE id IN (%s,%s,%s)", member_ids)
                await conn.commit()
        async with platform_pool.acquire() as conn:
            async with conn.cursor(aiomysql.Cursor) as cur:
                if config_existed:
                    await cur.execute("UPDATE _system_config SET config_value=%s WHERE config_key=%s",
                                      (prior_config, POSITION_ORDER_CONFIG))
                else:
                    await cur.execute("DELETE FROM _system_config WHERE config_key=%s",
                                      (POSITION_ORDER_CONFIG,))
            await conn.commit()
        online_pool.close()
        platform_pool.close()
        workflow_pool.close()
        await online_pool.wait_closed()
        await platform_pool.wait_closed()
        await workflow_pool.wait_closed()


@pytest.mark.asyncio
async def test_mysql_drinking_report_json_loads_idempotently_and_completes_after_resubmit():
    _require_isolated_databases()
    import aiomysql

    pool_options = {
        "host": settings.MYSQL_HOST,
        "port": settings.MYSQL_PORT,
        "user": settings.MYSQL_USER,
        "password": settings.MYSQL_PASSWORD,
        "minsize": 1,
        "maxsize": 2,
        "charset": "utf8mb4",
        "autocommit": False,
        "cursorclass": DomainRoutingCursor,
    }
    platform_pool = await aiomysql.create_pool(db=settings.MYSQL_PLATFORM_DB, **pool_options)
    workflow_pool = await aiomysql.create_pool(db=settings.MYSQL_WORKFLOW_DB, **pool_options)
    process_id = None
    fixture_user_id = fixture_member_id = department_id = None
    peer_user_id = manager_user_id = peer_member_id = manager_member_id = None
    try:
        async with platform_pool.acquire() as conn:
            async with conn.cursor(aiomysql.Cursor) as cur:
                (fixture_user_id, fixture_member_id, department_id, peer_user_id, manager_user_id,
                 peer_member_id, manager_member_id) = await _create_platform_fixtures(cur)
            await conn.commit()

        bundle_path = Path(__file__).resolve().parents[1] / "process_definitions" / "drinking_report.json"
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        async with workflow_pool.acquire() as conn:
            await conn.begin()
            async with conn.cursor() as cur:
                await ensure_workflow_schema(cur)
                first = await load_process_bundle(cur, bundle, actor_user_id=fixture_user_id + 3)
                second = await load_process_bundle(cur, bundle, actor_user_id=fixture_user_id + 3)
                assert first["process_version_changed"] is True
                assert second["process_version_changed"] is False
                assert first["process_definition_id"] == second["process_definition_id"]
                process_id = first["process_definition_id"]
            await conn.commit()

        async with workflow_pool.acquire() as conn:
            await conn.begin()
            async with conn.cursor() as cur:
                engine = ProcessEngine(cur)
                instance = await engine.start(
                    process_version_id=first["process_version_id"],
                    requester_user_id=fixture_user_id,
                    title="饮酒报备集成测试",
                    form_data={
                        "drinking_at": "2026-10-25",
                        "drinking_place": "合成地点",
                        "reason": "家庭聚会",
                        "travel_method": "公共交通",
                        "family_phone": "13800000000",
                        "unit_position_detail": "测试岗位",
                    },
                )
                instance_id = instance["id"]
                await cur.execute("SELECT status,assignees FROM process_tasks WHERE instance_id=%s", (instance_id,))
                task_status, assignees = await cur.fetchone()
                assert (task_status, json.loads(assignees)) == ("pending", [manager_user_id])
                await cur.execute("SELECT form_data FROM process_instances WHERE id=%s", (instance_id,))
                snapshot = json.loads((await cur.fetchone())[0])
                assert snapshot["reporter_name"] == "测试发起人"
                assert snapshot["direct_leader"] == manager_user_id
                await cur.execute("SELECT id FROM process_tasks WHERE instance_id=%s", (instance_id,))
                task_id = (await cur.fetchone())[0]
                rejected = await engine.decide(task_id=task_id, actor_user_id=manager_user_id, decision="reject", note="请补充说明")
                assert rejected["status"] == "returned"
                resubmitted = await engine.resubmit(
                    instance_id=instance_id,
                    requester_user_id=fixture_user_id,
                    form_data={**snapshot, "notes": "已补充"},
                )
                assert resubmitted["instance_id"] == instance_id
                completed = await engine.decide(
                    task_id=resubmitted["task_id"], actor_user_id=manager_user_id,
                    decision="approve", note="同意报备",
                )
                assert completed["status"] == "completed"
                await cur.execute("SELECT instance_no,form_data FROM process_instances WHERE id=%s", (instance_id,))
                instance_no, final_data = await cur.fetchone()
                assert instance_no == instance["instance_no"]
                assert json.loads(final_data)["notes"] == "已补充"
                await cur.execute("SELECT event_type,detail_json FROM process_events WHERE instance_id=%s ORDER BY id", (instance_id,))
                events = await cur.fetchall()
                event_types = [row[0] for row in events]
                assert "process_started" in event_types
                assert "task_returned" in event_types
                assert "task_resubmitted" in event_types
                assert "task_decided" in event_types
                snapshots = [json.loads(row[1]).get("form_snapshot") for row in events if row[0] in {"instance_submitted", "task_resubmitted"}]
                assert snapshots[0]["direct_leader"] == manager_user_id
                assert snapshots[1]["notes"] == "已补充"
            await conn.commit()
    finally:
        if process_id is not None:
            async with workflow_pool.acquire() as conn:
                async with conn.cursor(aiomysql.Cursor) as cur:
                    await cur.execute("SELECT id FROM process_instances WHERE process_definition_id=%s", (process_id,))
                    instance_ids = [row[0] for row in await cur.fetchall()]
                    if instance_ids:
                        marks = ",".join(["%s"] * len(instance_ids))
                        params = tuple(instance_ids)
                        await cur.execute(f"DELETE FROM process_events WHERE instance_id IN ({marks})", params)
                        await cur.execute(f"DELETE FROM process_tasks WHERE instance_id IN ({marks})", params)
                        await cur.execute(f"DELETE FROM process_instances WHERE id IN ({marks})", params)
                    await cur.execute("DELETE FROM process_versions WHERE process_definition_id=%s", (process_id,))
                    await cur.execute("DELETE FROM process_definitions WHERE id=%s", (process_id,))
                await conn.commit()
        if fixture_user_id is not None:
            async with platform_pool.acquire() as conn:
                async with conn.cursor(aiomysql.Cursor) as cur:
                    user_ids = (fixture_user_id, peer_user_id, manager_user_id, fixture_user_id + 3)
                    member_ids = (fixture_member_id, peer_member_id, manager_member_id)
                    await cur.execute("DELETE FROM _notifications WHERE user_id IN (%s,%s,%s,%s)", user_ids)
                    await cur.execute("DELETE FROM _users WHERE id IN (%s,%s,%s,%s)", user_ids)
                    await cur.execute("DELETE FROM _grid_member_department_links WHERE member_id IN (%s,%s,%s)", member_ids)
                    await cur.execute("DELETE FROM _grid_members WHERE id IN (%s,%s,%s)", member_ids)
                    await cur.execute("DELETE FROM _departments WHERE id=%s", (department_id,))
                await conn.commit()
        platform_pool.close()
        workflow_pool.close()
        await platform_pool.wait_closed()
        await workflow_pool.wait_closed()
