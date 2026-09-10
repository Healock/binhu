"""Fail-closed non-production startup policy, before any schema initialization."""
from contextlib import asynccontextmanager
import asyncio
import re
import aiomysql
from config import settings


def validate_target(config):
    environment = config.APP_ENVIRONMENT
    if environment not in {"development", "staging"}:
        raise ValueError("isolated environment required")
    prefix = "Dev_" if environment == "development" else "Staging_"
    names = [getattr(config, f"MYSQL_{domain}_DB") for domain in (
        "ONLINE_DATA", "ARCHIVE", "DAILY_REPORT", "PLATFORM", "VISIT", "DISPATCH", "REGISTRY", "WORKFLOW")]
    if len(set(names)) != 8 or any(not name.startswith(prefix) or not re.fullmatch(r'[A-Za-z0-9_]+', name) for name in names):
        raise ValueError("isolated database names required")
    if config.MYSQL_HOST != "environment-mysql" or config.MYSQL_USER != "environment_app":
        raise ValueError("isolated database endpoint required")
    if config.TXDOCS_ENABLED or not config.LOCAL_DATA_SOURCE_ENABLED:
        raise ValueError("local-only data required")
    if config.QMF_SOURCE_ACQUISITION_ENABLED or config.QMF_REGISTRATION_ENABLED or config.VENUE_CLOUD_SYNC_ENABLED or config.VENUE_CLOUD_PULL_ENABLED:
        raise ValueError("external acquisition must be disabled")


async def verify_database_identity():
    validate_target(settings)
    conn = await aiomysql.connect(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER, password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_ONLINE_DATA_DB, connect_timeout=5)
    try:
        async with conn.cursor() as cur:
            for domain in ("ONLINE_DATA", "ARCHIVE", "DAILY_REPORT", "PLATFORM", "VISIT", "DISPATCH", "REGISTRY", "WORKFLOW"):
                name = getattr(settings, f"MYSQL_{domain}_DB")
                await cur.execute(f"SELECT environment FROM `{name}`._environment_identity WHERE id=1")
                row = await cur.fetchone()
                if not row or row[0] != settings.APP_ENVIRONMENT:
                    raise ValueError("database environment identity mismatch")
    finally:
        conn.close()


@asynccontextmanager
async def isolated_lifespan():
    await verify_database_identity()
    from database import init_db, close_db
    from services.online_projection_jobs import run_online_projection_worker
    from services.online_summary_updates import run_online_summary_update_worker, stop_online_summary_update_processing
    from services.platform_performance import run_performance_sampler
    await init_db()
    # No production backup, external acquisition, archive or synchronization workers.
    tasks = [asyncio.create_task(worker()) for worker in (
        run_online_projection_worker, run_online_summary_update_worker, run_performance_sampler)]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await stop_online_summary_update_processing()
        await close_db()
