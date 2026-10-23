"""Opt-in integration coverage for OnlineData/PlatformData/WorkflowData routing.

Run only with dedicated database schemas whose names all end in ``_test``.
The suite creates and removes its fixture process rows, but leaves the schema
in place for repeatable runs.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import settings
from database import DomainRoutingCursor, db_manager
from services.domain_schema import ensure_workflow_schema
from services.process_engine import ProcessEngine, POSITION_ORDER_CONFIG


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


async def _create_platform_fixtures(cur) -> tuple[int, int, int]:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _users (
            id BIGINT PRIMARY KEY,
            member_id BIGINT DEFAULT NULL,
            role VARCHAR(40) NOT NULL DEFAULT 'user'
        ) ENGINE=InnoDB
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _grid_members (
            id BIGINT PRIMARY KEY,
            position VARCHAR(100) NOT NULL,
            status VARCHAR(20) NOT NULL
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
    user_id = member_id + 2_000_000_001
    department_id = member_id + 4_000_000_001
    await cur.execute(
        "INSERT INTO _grid_members (id,position,status) VALUES (%s,'组员','在岗')", (member_id,)
    )
    await cur.execute(
        "INSERT INTO _users (id,member_id,role) VALUES (%s,%s,'user'),(%s,NULL,'admin')",
        (user_id, member_id, user_id + 1),
    )
    await cur.execute(
        "INSERT INTO _grid_member_department_links (member_id,department_id) VALUES (%s,%s)",
        (member_id, department_id),
    )
    return user_id, member_id, department_id


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
    previous_online_pool = db_manager._pools.get("online_data")
    db_manager._pools["online_data"] = online_pool
    process_code = f"mysql_b1_{uuid.uuid4().hex[:16]}"
    process_id = None
    fixture_user_id = fixture_member_id = None
    prior_config = None
    config_existed = False
    try:
        async with online_pool.acquire() as conn:
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
            await conn.commit()

        async with platform_pool.acquire() as conn:
            async with conn.cursor(aiomysql.Cursor) as cur:
                fixture_user_id, fixture_member_id, _ = await _create_platform_fixtures(cur)
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
                process_id = await engine.create_process(code=process_code, name="跨库超时集成测试")
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
                await cur.execute("SELECT escalation_stage,status FROM process_tasks WHERE instance_id=%s",
                                  (instance_id,))
                assert await cur.fetchone() == (1, "admin_hold")
                await cur.execute("SELECT status FROM process_instances WHERE id=%s", (instance_id,))
                assert (await cur.fetchone())[0] == "admin_hold"
            await conn.commit()
    finally:
        if process_id is not None:
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
                    await cur.execute("DELETE FROM _notifications WHERE user_id IN (%s,%s)",
                                      (fixture_user_id, fixture_user_id + 1))
                    await cur.execute("DELETE FROM _users WHERE id IN (%s,%s)",
                                      (fixture_user_id, fixture_user_id + 1))
                    await cur.execute("DELETE FROM _grid_member_department_links WHERE member_id=%s",
                                      (fixture_member_id,))
                    await cur.execute("DELETE FROM _grid_members WHERE id=%s", (fixture_member_id,))
                await conn.commit()
        async with online_pool.acquire() as conn:
            async with conn.cursor(aiomysql.Cursor) as cur:
                if config_existed:
                    await cur.execute("UPDATE _system_config SET config_value=%s WHERE config_key=%s",
                                      (prior_config, POSITION_ORDER_CONFIG))
                else:
                    await cur.execute("DELETE FROM _system_config WHERE config_key=%s",
                                      (POSITION_ORDER_CONFIG,))
            await conn.commit()
        if previous_online_pool is None:
            db_manager._pools.pop("online_data", None)
        else:
            db_manager._pools["online_data"] = previous_online_pool
        online_pool.close()
        platform_pool.close()
        workflow_pool.close()
        await online_pool.wait_closed()
        await platform_pool.wait_closed()
        await workflow_pool.wait_closed()
