"""Create a read-only, privacy-minimized household identity evidence artifact."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

from config import settings
from migrations.registry_issue_reconciliation import open_connection, close_connection
from services.registry_identity_evidence import (
    discover_reference_inventory,
    scan_references,
    seal_artifact,
)
from services.registry_identity_maintenance import plan_identity_merges


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


async def build_evidence(cur, schemas: list[str], registry_schema: str, property_ids: list[int], run_id: str):
    inventory, opaque_json = await discover_reference_inventory(cur, schemas, registry_schema)
    await cur.execute(
        "SELECT id,community_id,street,natural_address,building,room,normalized_address,"
        "source_house_no,source_updated_at,housing_type,residence_type,household_status,source_type,status,current_version "
        "FROM registry_properties ORDER BY id"
    )
    all_rows = list(await cur.fetchall())
    requested = set(property_ids)
    if requested:
        matching_keys = {(row[1], row[6]) for row in all_rows if row[0] in requested}
        all_rows = [row for row in all_rows if (row[1], row[6]) in matching_keys]
    counts = {}
    for row in all_rows:
        key = (row[1], row[6])
        counts[key] = counts.get(key, 0) + 1
    duplicate_rows = [row for row in all_rows if counts[(row[1], row[6])] > 1]
    planned_pairs, identity_blocked_groups = plan_identity_merges(duplicate_rows)
    candidate_property_ids = sorted({int(row[0]) for row in duplicate_rows})
    references = await scan_references(cur, inventory, candidate_property_ids)
    refs_by_property = {}
    for reference in references:
        refs_by_property.setdefault(reference["property_id"], []).append(reference)

    candidates = []
    for old_id, canonical_id in planned_pairs:
        old_refs = refs_by_property.get(old_id, [])
        blockers = []
        certificate_refs = [ref for ref in old_refs if (
                ref["schema"] == registry_schema and ref["table"] == "registry_property_certificates"
                and ref["column"] == "property_id"
            )]
        blocking_refs = [ref for ref in old_refs if ref["kind"] in {"direct", "polymorphic"} and ref not in certificate_refs]
        if blocking_refs:
            reference_disposition = "blocked_references"
            blockers.append("non_certificate_reference")
        elif certificate_refs:
            reference_disposition = "certificate_only_exception_candidate"
        else:
            reference_disposition = "no_references_candidate"
        if any(not ref["stable_record_keys"] for ref in old_refs):
            blockers.append("reference_without_stable_record_key")
        await cur.execute(
            "SELECT match_status,confirmed_by,confirmed_at FROM registry_property_small_community_links WHERE property_id=%s",
            (old_id,),
        )
        link = await cur.fetchone()
        if link and (link[1] is not None or link[2] is not None or link[0] == "confirmed"):
            blockers.append("human_confirmed_small_community")
        disposition = "blocked" if blockers else reference_disposition
        candidates.append({
            "old_property_id": old_id,
            "canonical_property_id": canonical_id,
            "disposition": disposition,
            "reference_disposition": reference_disposition,
            "blockers": sorted(set(blockers)),
            "reference_count": sum(ref["count"] for ref in old_refs),
        })

    grouped_ids = {}
    for row in duplicate_rows:
        grouped_ids.setdefault((row[1], row[6]), []).append(int(row[0]))
    candidate_groups = [
        {"property_ids": sorted(ids), "identity_shape": "eligible" if any(
            candidate["old_property_id"] in ids and candidate["disposition"] != "blocked"
            for candidate in candidates
        ) else "blocked"}
        for ids in sorted(grouped_ids.values(), key=lambda values: tuple(sorted(values)))
    ]
    return {
        "schema_version": 1,
        "artifact_type": "registry_property_identity_readonly_evidence",
        "run_id": run_id,
        "environment": settings.APP_ENVIRONMENT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database_scope": sorted(schemas),
        "candidate_group_count": len(candidate_groups),
        "identity_shape_blocked_group_count": identity_blocked_groups,
        "candidate_pair_count": len(candidates),
        "certificate_only_exception_candidate_count": sum(
            candidate["disposition"] == "certificate_only_exception_candidate" for candidate in candidates
        ),
        "no_references_candidate_count": sum(
            candidate["disposition"] == "no_references_candidate" for candidate in candidates
        ),
        "blocked_candidate_count": sum(candidate["disposition"] == "blocked" for candidate in candidates),
        "reference_inventory": inventory,
        "references": references,
        "candidates": candidates,
        "candidate_groups": candidate_groups,
        "opaque_json_column_count": len(opaque_json),
        "opaque_json_columns": opaque_json,
        "coverage": "column_and_discriminator_scan; JSON payload contents are not interpreted",
        "execution_available": False,
        "read_only": True,
    }


async def run(args):
    if args.environment != settings.APP_ENVIRONMENT:
        raise ValueError("environment_identity_mismatch")
    validate_database_scope()
    if any(identifier <= 0 for identifier in args.property_id):
        raise ValueError("invalid_property_identity")
    schemas = database_scope()
    conn = await open_connection(settings.MYSQL_REGISTRY_DB)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SET SESSION TRANSACTION READ ONLY")
            await cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            await cur.execute(
                "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN ("
                + ",".join(["%s"] * len(schemas)) + ")", tuple(schemas),
            )
            if {row[0] for row in await cur.fetchall()} != set(schemas):
                raise ValueError("database_scope_not_fully_visible")
            if settings.APP_ENVIRONMENT != "production":
                for schema in schemas:
                    await cur.execute(f"SELECT id,environment FROM `{schema}`._environment_identity")
                    if list(await cur.fetchall()) != [(1, settings.APP_ENVIRONMENT)]:
                        raise ValueError("database_identity_marker_mismatch")
            run_id = args.run_id or f"registry-identity-evidence-{uuid.uuid4().hex}"
            artifact = seal_artifact(await build_evidence(
                cur, schemas, settings.MYSQL_REGISTRY_DB, args.property_id, run_id
            ))
            return artifact
    finally:
        try:
            await conn.rollback()
        finally:
            await close_connection(conn)


def write_exclusive(path: str, artifact: dict):
    target = Path(path)
    data = (json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(str(target), flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--environment", required=True, choices=("production", "staging", "development"))
    result.add_argument("--property-id", action="append", type=int, default=[])
    result.add_argument("--run-id", help="Non-sensitive evidence run identifier")
    result.add_argument("--output", help="Create a new evidence file; existing files are never overwritten")
    return result


if __name__ == "__main__":
    parsed_args = parser().parse_args()
    try:
        outcome = asyncio.run(run(parsed_args))
        if parsed_args.output:
            write_exclusive(parsed_args.output, outcome)
            print(json.dumps({"status": "created", "artifact_sha256": outcome["artifact_sha256"],
                              "candidate_group_count": outcome["candidate_group_count"],
                              "candidate_pair_count": outcome["candidate_pair_count"],
                              "read_only": True}, ensure_ascii=False))
        else:
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "reason": "readonly_evidence_generation_aborted"}))
        raise SystemExit(1) from None
