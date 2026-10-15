"""Controlled household identity consolidation: measure -> migrate --apply -> verify.

Run only in an approved maintenance window with RegistryData/OnlineData backups.
Outputs contain internal IDs, counts and hashes, never protected snapshots.
"""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from config import settings
from migrations.registry_issue_reconciliation import open_connection, close_connection
from services.registry_identity_maintenance import prepare_merges, apply_merge, dependent_references


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
    if args.environment != settings.APP_ENVIRONMENT:
        raise ValueError("environment_identity_mismatch")
    validate_database_scope()
    if any(identifier <= 0 for identifier in args.property_id):
        raise ValueError("invalid_property_identity")
    apply = args.command == "migrate" and args.apply
    if apply and (not args.property_id or not args.plan_token or not args.backup_reference
                  or not Path(args.backup_reference).is_file()):
        raise ValueError("apply_requires_explicit_properties_measurement_and_backup_reference")
    conn = await open_connection(settings.MYSQL_REGISTRY_DB)
    try:
        async with conn.cursor() as cur:
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
            if apply:
                if token != args.plan_token:
                    raise ValueError("measurement_changed_rerun_measure")
                if blocked:
                    raise ValueError("selected_properties_require_manual_review")
                run_id = str(uuid4())
                for old_id, current_id in ready:
                    await apply_merge(cur, old_id=old_id, current_id=current_id, run_id=run_id)
                await conn.commit()
            else:
                await conn.rollback()
            result = {"command": args.command, "environment": settings.APP_ENVIRONMENT,
                      "eligible": len(ready), "blocked": blocked, "plan_token": token,
                      "pairs": [{"old_id": old, "canonical_id": current} for old, current in ready],
                      "applied": len(ready) if apply else 0}
            if args.command == "verify":
                await cur.execute("SELECT old_property_id,canonical_property_id FROM registry_property_identity_merges ORDER BY old_property_id")
                failures = 0
                for old_id, current_id in await cur.fetchall():
                    if args.property_id and not ({old_id, current_id} & set(args.property_id)):
                        continue
                    await cur.execute("SELECT id FROM registry_properties WHERE id IN (%s,%s)", (old_id, current_id))
                    if [int(row[0]) for row in await cur.fetchall()] != [int(current_id)]:
                        failures += 1
                    await cur.execute("SELECT 1 FROM registry_source_records WHERE entity_type='household_property' AND entity_id=%s LIMIT 1", (old_id,))
                    if await cur.fetchone():
                        failures += 1
                    if await dependent_references(cur, schemas=database_scope(), registry_schema="", old_id=old_id):
                        failures += 1
                result["verification_failures"] = failures
                result["verified"] = not ready and not failures and not blocked
                await conn.rollback()
        return result
    except Exception:
        await conn.rollback()
        raise
    finally:
        await close_connection(conn)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("measure", "migrate", "verify"))
    result.add_argument("--environment", required=True, choices=("production", "staging", "development"))
    result.add_argument("--property-id", action="append", type=int, default=[])
    result.add_argument("--apply", action="store_true")
    result.add_argument("--plan-token")
    result.add_argument("--backup-reference")
    return result


if __name__ == "__main__":
    try:
        outcome = asyncio.run(run(parser().parse_args()))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "reason": "maintenance_aborted_review_measurement_and_backup"}))
        raise SystemExit(1) from None
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    if outcome.get("verified") is False:
        raise SystemExit(1)
