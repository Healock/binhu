"""Controlled legacy weekend -> daily schedule migration; no source deletion.

Run with the backend's target environment configuration, in a maintenance window:
measure --environment staging; migrate --apply ...; verify --environment staging.
"""

from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
import aiomysql
from config import settings
from services.attendance_schedule import table, MIGRATION_KEY, ready


def converted_rows(rows):
    result = []
    for member, week, duty, actor in rows:
        if week.weekday() != 0 or (
            duty is not None
            and duty not in (week + timedelta(days=5), week + timedelta(days=6))
        ):
            raise ValueError("legacy_schedule_invalid")
        for offset in (5, 6):
            day = week + timedelta(days=offset)
            result.append((int(member), day, "work" if day == duty else "rest", actor))
    return result


def fingerprint(rows):
    return hashlib.sha256(
        json.dumps(rows, default=str, separators=(",", ":")).encode()
    ).hexdigest()


async def measure(cur):
    await cur.execute(
        f"SELECT member_id,week_start,duty_date,updated_by FROM {table('_personnel_weekend_duty')} ORDER BY member_id,week_start"
    )
    rows = await cur.fetchall()
    converted = converted_rows(rows)
    return (
        dict(
            legacy_count=len(rows),
            expected_daily_count=len(converted),
            measurement_hash=fingerprint(rows),
        ),
        converted,
    )


async def verify(cur):
    report, expected = await measure(cur)
    await cur.execute(
        f"SELECT config_value FROM {table('_system_config')} WHERE config_key='attendance_migration_evidence'"
    )
    saved = await cur.fetchone()
    evidence = json.loads(saved[0]) if saved else {}
    baseline_matches = all(evidence.get(k) == report[k] for k in report)
    await cur.execute(
        f"SELECT member_id,work_date,state,source,revision FROM {table('_personnel_work_schedule')}"
    )
    actual = {
        (int(m), d): (s, source, int(v)) for m, d, s, source, v in await cur.fetchall()
    }
    await cur.execute(
        f"SELECT member_id,revision,after_json FROM {table('_personnel_attendance_changes')} WHERE action IN ('schedule','weekend')"
    )
    changed = {}
    for m, revision, body in await cur.fetchall():
        changes = json.loads(body) if isinstance(body, str) else body
        for item in changes:
            changed[int(m), str(item["date"]), int(revision)] = item["after"]
    missing = sum((m, d) not in actual for m, d, _, _ in expected)
    mismatches, edited = 0, 0
    for m, d, state, _ in expected:
        row = actual.get((m, d))
        if row is None:
            continue
        if row[1] == "legacy_weekend":
            mismatches += row[0] != state or row[2] != 0
        else:
            valid = changed.get((m, str(d), row[2])) == row[0]
            mismatches += not valid
            edited += valid
    expected_keys = {(m, d) for m, d, _, _ in expected}
    extra_legacy = sum(
        source == "legacy_weekend" and key not in expected_keys
        for key, (_, source, _) in actual.items()
    )
    enabled = await ready(cur)
    return dict(
        **report,
        missing=missing,
        mismatches=mismatches,
        edited_since_migration=edited,
        extra_legacy=extra_legacy,
        baseline_matches=baseline_matches,
        enabled=enabled,
        verified=enabled
        and baseline_matches
        and not missing
        and not mismatches
        and not extra_legacy,
    )


def check_backup(path, environment, schema):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("environment") != environment or manifest.get("schema") != schema:
        raise ValueError("backup_identity_mismatch")
    dump = Path(manifest["path"])
    h = hashlib.sha256()
    with dump.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    if not dump.stat().st_size or h.hexdigest() != manifest.get("sha256"):
        raise ValueError("backup_hash_mismatch")


async def run(args):
    if settings.APP_ENVIRONMENT != args.environment:
        raise ValueError("environment_mismatch")
    schema = (
        settings.MYSQL_PLATFORM_DB
        if settings.PLATFORM_DOMAIN_ACTIVE
        else settings.MYSQL_ONLINE_DATA_DB
    )
    conn = await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=schema,
        autocommit=False,
        charset="utf8mb4",
    )
    try:
        async with conn.cursor() as cur:
            # Serializes migration commands. Runtime writers remain gated by the
            # activation marker until this entire transaction has committed.
            await cur.execute(
                "SELECT GET_LOCK(%s,10)", ("binhu-attendance-migration-" + schema,)
            )
            if (await cur.fetchone())[0] != 1:
                raise ValueError("migration_busy")
            if args.command == "verify":
                result = await verify(cur)
                if not result["verified"]:
                    raise ValueError("verification_failed")
            elif args.command == "measure":
                result, _ = await measure(cur)
            else:
                if (
                    not args.apply
                    or not args.maintenance_confirmed
                    or not args.run_id
                    or not args.backup_manifest
                ):
                    raise ValueError("apply_requires_backup_run_id_and_maintenance")
                check_backup(args.backup_manifest, args.environment, schema)
                await cur.execute(
                    f"SELECT config_key,config_value FROM {table('_system_config')} WHERE config_key IN ('maintenance_enabled','maintenance_start_at','maintenance_end_at','maintenance_message','timezone')"
                )
                from services.maintenance import maintenance_status

                if not maintenance_status(dict(await cur.fetchall()))["active"]:
                    raise ValueError("maintenance_not_active")
                await conn.begin()
                report, rows = await measure(cur)
                if (
                    report["measurement_hash"] != args.measurement_hash
                    or report["expected_daily_count"] != args.expected_count
                ):
                    raise ValueError("measurement_changed")
                if not await ready(cur):
                    await cur.execute(
                        f"SELECT COUNT(*) FROM {table('_personnel_work_schedule')}"
                    )
                    if (await cur.fetchone())[0]:
                        raise ValueError("unexpected_existing_daily_schedule")
                    if rows:
                        await cur.executemany(
                            f"INSERT INTO {table('_personnel_work_schedule')} "
                            "(member_id,work_date,state,source,revision,updated_by) VALUES (%s,%s,%s,'legacy_weekend',0,%s)",
                            rows,
                        )
                    workflow_schema = settings.MYSQL_WORKFLOW_DB.replace("`", "")
                    await cur.execute(
                        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s AND table_name='leave_request_details'",
                        (workflow_schema,),
                    )
                    if (await cur.fetchone())[0]:
                        await cur.execute(
                            f"UPDATE {table('_personnel_attendance_history')} h JOIN `{workflow_schema}`.leave_request_details d ON d.attendance_record_id=h.id SET h.workflow_ticket_id=d.work_order_id WHERE h.source='workflow' AND h.workflow_ticket_id IS NULL"
                        )
                    await cur.execute(
                        f"SELECT COUNT(*) FROM {table('_personnel_attendance_history')} WHERE source='workflow' AND workflow_ticket_id IS NULL"
                    )
                    if (await cur.fetchone())[0]:
                        raise ValueError("workflow_link_unavailable")
                    for key, value in [
                        (MIGRATION_KEY, "1"),
                        ("attendance_legacy_seed_v1", "1"),
                        ("attendance_migration_run", args.run_id),
                        (
                            "attendance_migration_evidence",
                            json.dumps(report, sort_keys=True),
                        ),
                    ]:
                        await cur.execute(
                            f"INSERT INTO {table('_system_config')} (config_key,config_value) VALUES (%s,%s) "
                            "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)",
                            (key, value),
                        )
                    result = await verify(cur)
                    if not result["verified"]:
                        raise ValueError("verification_failed")
                    await conn.commit()
                else:
                    result = await verify(cur)
                    if not result["verified"]:
                        raise ValueError("verification_failed")
            print(
                json.dumps(
                    dict(environment=args.environment, **result), ensure_ascii=False
                )
            )
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["measure", "migrate", "verify"])
    parser.add_argument(
        "--environment", required=True, choices=["production", "staging", "development"]
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--maintenance-confirmed", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--measurement-hash")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--backup-manifest")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as exc:
        # Fixed diagnostics only: never print database connection errors or rows.
        allowed = {
            "legacy_schedule_invalid",
            "backup_identity_mismatch",
            "backup_hash_mismatch",
            "environment_mismatch",
            "migration_busy",
            "verification_failed",
            "apply_requires_backup_run_id_and_maintenance",
            "measurement_changed",
            "unexpected_existing_daily_schedule",
            "maintenance_not_active",
            "workflow_link_unavailable",
        }
        code = (
            str(exc)
            if isinstance(exc, ValueError) and str(exc) in allowed
            else type(exc).__name__
        )
        print(json.dumps(dict(status="failed", code=code)))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
