"""Read-only household identity measurement; no consolidation executor is provided.

Outputs contain internal IDs, counts and hashes, never protected snapshots.
Historical consolidation requires a separately reviewed maintenance gateway.
"""
import argparse
import asyncio
import json

from config import settings
from migrations.registry_issue_reconciliation import open_connection, close_connection
from services.registry_identity_maintenance import prepare_merges


def database_scope():
    return sorted({getattr(settings, name) for name in (
        "MYSQL_REGISTRY_DB", "MYSQL_ONLINE_DATA_DB", "MYSQL_ARCHIVE_DB", "MYSQL_PLATFORM_DB",
        "MYSQL_VISIT_DB", "MYSQL_DISPATCH_DB", "MYSQL_WORKFLOW_DB", "MYSQL_DAILY_REPORT_DB",
    )})


def validate_database_scope():
    prefix = {"production": "", "development": "Dev_", "staging": "Staging_"}[settings.APP_ENVIRONMENT]
    expected = {prefix + name for name in (
        "RegistryData", "OnlineData", "OnlineDataArchive", "PlatformData", "VisitData",
        "DispatchData", "WorkflowData", "daily_report",
    )}
    if set(database_scope()) != expected:
        raise ValueError("database_environment_scope_mismatch")


async def run(args):
    if args.command not in {"measure", "verify"}:
        raise ValueError("read_only_commands_only")
    if args.environment != settings.APP_ENVIRONMENT:
        raise ValueError("environment_identity_mismatch")
    validate_database_scope()
    if any(identifier <= 0 for identifier in args.property_id):
        raise ValueError("invalid_property_identity")
    conn = await open_connection(settings.MYSQL_REGISTRY_DB)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SET SESSION TRANSACTION READ ONLY")
            await cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            await cur.execute(
                "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN ("
                + ",".join(["%s"] * len(database_scope())) + ")", tuple(database_scope()),
            )
            if {row[0] for row in await cur.fetchall()} != set(database_scope()):
                raise ValueError("database_scope_not_fully_visible")
            if settings.APP_ENVIRONMENT != "production":
                for schema in database_scope():
                    await cur.execute(f"SELECT id,environment FROM `{schema}`._environment_identity")
                    if list(await cur.fetchall()) != [(1, settings.APP_ENVIRONMENT)]:
                        raise ValueError("database_identity_marker_mismatch")
            ready, blocked, token = await prepare_merges(
                cur, schemas=database_scope(), registry_schema=settings.MYSQL_REGISTRY_DB,
                property_ids=args.property_id,
            )
            result = {"command": args.command, "environment": settings.APP_ENVIRONMENT,
                      "eligible": len(ready), "blocked": blocked, "measurement_hash": token,
                      "pairs": [{"old_id": old, "canonical_id": current} for old, current in ready],
                      "read_only": True, "execution_available": False}
            if args.command == "verify":
                result["verified"] = not ready and not blocked
            return result
    finally:
        try:
            await conn.rollback()
        finally:
            await close_connection(conn)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("measure", "verify"))
    result.add_argument("--environment", required=True, choices=("production", "staging", "development"))
    result.add_argument("--property-id", action="append", type=int, default=[])
    return result


if __name__ == "__main__":
    try:
        outcome = asyncio.run(run(parser().parse_args()))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "reason": "read_only_measurement_aborted"}))
        raise SystemExit(1) from None
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    if outcome.get("verified") is False:
        raise SystemExit(1)
