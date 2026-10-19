"""Controlled one-shot maintenance for one registry-property identity pair.

This command is intentionally narrower than a general migration runner.  It
accepts one measured old/current pair, never accepts SQL or table names, and
only applies the certificate-only exception (or a pair with no active
references).  It is invoked by the existing production-maintenance gateway;
the module itself is not an HTTP service.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings
from migrations.registry_issue_reconciliation import close_connection, open_connection
from services.registry_identity_evidence import (
    discover_reference_inventory,
    scan_certificate_source_references,
    scan_opaque_json_reference_markers,
    scan_references,
    seal_artifact,
)
from services.registry_identity_maintenance import plan_identity_merges


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")
ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
LOCK_NAME = "binhu-registry-property-identity"
CONFIRM_APPLY = "confirm-registry-property-identity"
CONFIRM_ROLLBACK = "confirm-registry-property-identity-rollback"
REFERENCE_EVENT_TABLE = "registry_property_identity_reference_events"
MAX_PROPERTY_ID = 9223372036854775807
MAX_QUERY_EXECUTION_MS = 30000
METADATA_LOCK_WAIT_SECONDS = 2
INNODB_LOCK_WAIT_SECONDS = 5
BACKUP_ROOT = Path("/root/binhu/deploy-backups/automated")
REQUIRED_MAPPING_COLUMNS = {
    "old_property_id", "canonical_property_id", "community_id", "status", "version",
    "maintenance_run_id", "migration_measurement_hash", "post_apply_measurement_hash",
    "post_rollback_measurement_hash", "candidate_class", "maintenance_actor",
}
REQUIRED_EVENT_COLUMNS = {
    "maintenance_run_id", "maintenance_actor", "event_type", "mapping_version", "reason_code",
}
REQUIRED_REFERENCE_EVENT_COLUMNS = {
    "maintenance_run_id", "old_property_id", "canonical_property_id", "table_name",
    "column_name", "record_id", "record_hash",
}


class IdentityMaintenanceError(RuntimeError):
    pass


def database_scope() -> list[str]:
    return sorted({getattr(settings, name) for name in (
        "MYSQL_REGISTRY_DB", "MYSQL_ONLINE_DATA_DB", "MYSQL_ARCHIVE_DB", "MYSQL_PLATFORM_DB",
        "MYSQL_VISIT_DB", "MYSQL_DISPATCH_DB", "MYSQL_WORKFLOW_DB", "MYSQL_DAILY_REPORT_DB",
    )})


def validate_database_scope() -> None:
    prefix = {"production": "", "development": "Dev_", "staging": "Staging_"}[settings.APP_ENVIRONMENT]
    expected = {prefix + name for name in (
        "RegistryData", "OnlineData", "OnlineDataArchive", "PlatformData", "VisitData",
        "DispatchData", "WorkflowData", "daily_report",
    )}
    if set(database_scope()) != expected:
        raise IdentityMaintenanceError("database_environment_scope_mismatch")


def validate_args(*, run_id: str, old_property_id: int, canonical_property_id: int, actor: str) -> None:
    if not RUN_ID_RE.fullmatch(run_id):
        raise IdentityMaintenanceError("invalid_run_id")
    if not ACTOR_RE.fullmatch(actor):
        raise IdentityMaintenanceError("invalid_maintenance_actor")
    if (
        old_property_id <= 0 or canonical_property_id <= 0
        or old_property_id > MAX_PROPERTY_ID or canonical_property_id > MAX_PROPERTY_ID
        or old_property_id == canonical_property_id
    ):
        raise IdentityMaintenanceError("invalid_property_pair")


async def _check_scope(cur) -> None:
    schemas = database_scope()
    await cur.execute(
        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN (" +
        ",".join(["%s"] * len(schemas)) + ")", tuple(schemas),
    )
    if {row[0] for row in await cur.fetchall()} != set(schemas):
        raise IdentityMaintenanceError("database_scope_not_fully_visible")
    if settings.APP_ENVIRONMENT != "production":
        for schema in schemas:
            await cur.execute(f"SELECT id,environment FROM `{schema}`._environment_identity")
            if list(await cur.fetchall()) != [(1, settings.APP_ENVIRONMENT)]:
                raise IdentityMaintenanceError("database_identity_marker_mismatch")


async def _set_query_budget(cur) -> None:
    await cur.execute("SET SESSION MAX_EXECUTION_TIME=%s", (MAX_QUERY_EXECUTION_MS,))
    await cur.execute("SET SESSION lock_wait_timeout=%s", (METADATA_LOCK_WAIT_SECONDS,))
    await cur.execute("SET SESSION innodb_lock_wait_timeout=%s", (INNODB_LOCK_WAIT_SECONDS,))


def _json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _address_hmac(value: Any) -> str:
    key = settings.registry_hmac_key.encode("utf-8")
    message = b"registry-property-identity-v1\0" + str(value or "").encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _candidate_rows(cur, old_id: int, canonical_id: int, *, for_update: bool = False) -> list[tuple]:
    lock_clause = " FOR UPDATE" if for_update else ""
    await cur.execute(
        "SELECT id,community_id,street,natural_address,building,room,normalized_address,"
        "source_house_no,source_updated_at,housing_type,residence_type,household_status,"
        "source_type,status,current_version FROM registry_properties "
        "WHERE id IN (%s,%s) ORDER BY id" + lock_clause, (old_id, canonical_id),
    )
    selected = list(await cur.fetchall())
    if len(selected) != 2:
        raise IdentityMaintenanceError("property_pair_missing")
    key = (selected[0][1], selected[0][6])
    if not key[0] or not key[1]:
        raise IdentityMaintenanceError("property_identity_incomplete")
    await cur.execute(
        "SELECT id,community_id,street,natural_address,building,room,normalized_address,"
        "source_house_no,source_updated_at,housing_type,residence_type,household_status,"
        "source_type,status,current_version FROM registry_properties "
        "WHERE community_id=%s AND normalized_address=%s ORDER BY id" + lock_clause, key,
    )
    group = list(await cur.fetchall())
    if {int(row[0]) for row in group} != {old_id, canonical_id}:
        raise IdentityMaintenanceError("property_group_not_exactly_two")
    pairs, _ = plan_identity_merges(group)
    if pairs != [(old_id, canonical_id)]:
        raise IdentityMaintenanceError("property_pair_not_measured_candidate")
    return group


async def _reference_summary(cur, old_id: int, canonical_id: int) -> dict[str, Any]:
    inventory, opaque_json, warnings = await discover_reference_inventory(
        cur, database_scope(), settings.MYSQL_REGISTRY_DB,
    )
    references = await scan_references(cur, inventory, [old_id, canonical_id])
    references.extend(await scan_certificate_source_references(
        cur, registry_schema=settings.MYSQL_REGISTRY_DB, property_ids=[old_id, canonical_id],
    ))
    opaque_matches = await scan_opaque_json_reference_markers(
        cur, opaque_json, [old_id, canonical_id],
    )
    old_refs = [item for item in references if int(item["property_id"]) == old_id]
    current_refs = [item for item in references if int(item["property_id"]) == canonical_id]
    old_certificates = [item for item in old_refs if item["table"] == "registry_property_certificates" and item["kind"] == "direct"]
    old_certificate_sources = [item for item in old_refs if item["kind"] == "certificate_source_reference"]
    current_certificates = [item for item in current_refs if item["table"] == "registry_property_certificates" and item["kind"] == "direct"]
    current_certificate_sources = [item for item in current_refs if item["kind"] == "certificate_source_reference"]
    blocking_old = [item for item in old_refs if item not in old_certificates and item["kind"] in {"direct", "polymorphic"}]
    await cur.execute(
        "SELECT property_id,small_community_id,match_status,confirmed_by,confirmed_at "
        "FROM registry_property_small_community_links WHERE property_id IN (%s,%s)",
        (old_id, canonical_id),
    )
    links = list(await cur.fetchall())
    old_links = [row for row in links if int(row[0]) == old_id]
    old_link_blocked = bool(old_links)
    opaque_reference_count = sum(int(item["count"]) for item in opaque_matches)
    opaque_old_reference_count = sum(
        int(item["count"]) for item in opaque_matches if int(item["property_id"]) == old_id
    )
    if blocking_old or old_link_blocked or opaque_old_reference_count or warnings:
        disposition = "blocked"
    elif old_certificates:
        disposition = "certificate_only_exception_candidate"
    else:
        disposition = "no_references_candidate"
    if len(old_certificates) > 1 or len(old_certificate_sources) > 1:
        disposition = "blocked"
    summary = {
        "old_property_id": old_id,
        "canonical_property_id": canonical_id,
        "disposition": disposition,
        "old_reference_count": sum(int(item["count"]) for item in old_refs),
        "canonical_reference_count": sum(int(item["count"]) for item in current_refs),
        "old_certificate_count": sum(int(item["count"]) for item in old_certificates),
        "old_certificate_source_count": sum(int(item["count"]) for item in old_certificate_sources),
        "canonical_certificate_count": sum(int(item["count"]) for item in current_certificates),
        "canonical_certificate_source_count": sum(int(item["count"]) for item in current_certificate_sources),
        "blocking_reference_count": len(blocking_old),
        "old_small_community_link_count": len(old_links),
        "opaque_json_column_count": len(opaque_json),
        "opaque_json_scanned_count": len(opaque_matches),
        "opaque_json_reference_count": opaque_reference_count,
        "opaque_json_old_reference_count": opaque_old_reference_count,
        "opaque_json_scan": opaque_matches,
        "unknown_polymorphic_type_count": len(warnings),
        "unknown_polymorphic_types": warnings,
        "reference_inventory_complete": not warnings,
        "opaque_json_matches": [item for item in opaque_matches if int(item["count"]) > 0],
        "references": sorted(references, key=lambda item: (
            int(item["property_id"]), item["schema"], item["table"], item["column"], item["kind"],
        )),
    }
    if disposition == "certificate_only_exception_candidate" and (
        summary["old_certificate_count"] != 1 or summary["old_certificate_source_count"] != 1
        or summary["canonical_certificate_count"] != 0
        or summary["canonical_certificate_source_count"] != 0
    ):
        summary["disposition"] = "blocked"
    return summary


async def _measure_cursor(cur, *, run_id: str, actor: str, old_id: int, canonical_id: int,
                          for_update: bool = False) -> dict[str, Any]:
    group = await _candidate_rows(cur, old_id, canonical_id, for_update=for_update)
    references = await _reference_summary(cur, old_id, canonical_id)
    properties = [{
        "id": int(row[0]), "community_id": int(row[1]),
        "natural_address_hmac": _address_hmac(row[3]),
        "normalized_address_hmac": _address_hmac(row[6]),
        "source_house_no": bool(str(row[7] or "")), "source_updated_at": str(row[8] or ""),
        "household_status": str(row[11] or ""), "source_type": str(row[12] or ""),
        "status": str(row[13] or ""), "current_version": int(row[14] or 0),
    } for row in sorted(group, key=lambda item: int(item[0]))]
    token_input = {
        "database_scope": database_scope(), "properties": properties,
        "pair": [old_id, canonical_id], "reference_summary": references,
    }
    measurement_hash = _json_digest(token_input)
    return seal_artifact({
        "schema_version": 1,
        "artifact_type": "registry_property_identity_single_group_measurement",
        "run_id": run_id,
        "maintenance_actor": actor,
        "environment": settings.APP_ENVIRONMENT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "old_property_id": old_id,
        "canonical_property_id": canonical_id,
        "measurement_hash": measurement_hash,
        "disposition": references["disposition"],
        "properties": properties,
        "reference_summary": {key: value for key, value in references.items() if key != "references"},
        "references": references["references"],
        "opaque_json_scan": references["opaque_json_scan"],
        "read_only": True,
        "execution_available": references["disposition"] in {"no_references_candidate", "certificate_only_exception_candidate"},
    })


async def run_measure(*, run_id: str, old_property_id: int, canonical_property_id: int, actor: str) -> dict[str, Any]:
    validate_args(run_id=run_id, old_property_id=old_property_id, canonical_property_id=canonical_property_id, actor=actor)
    validate_database_scope()
    conn = await open_connection(settings.MYSQL_REGISTRY_DB)
    try:
        async with conn.cursor() as cur:
            await _set_query_budget(cur)
            await cur.execute("SET SESSION TRANSACTION READ ONLY")
            await cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            await _check_scope(cur)
            return await _measure_cursor(cur, run_id=run_id, actor=actor, old_id=old_property_id, canonical_id=canonical_property_id)
    finally:
        try:
            await conn.rollback()
        finally:
            await close_connection(conn)


def _verify_manifest(path: str, *, run_id: str) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    try:
        manifest_path.relative_to(BACKUP_ROOT.resolve())
    except ValueError as exc:
        raise IdentityMaintenanceError("backup_manifest_outside_fixed_root") from exc
    if manifest_path.name != "backup-manifest.json" or not manifest_path.is_file():
        raise IdentityMaintenanceError("backup_manifest_missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IdentityMaintenanceError("backup_manifest_invalid") from exc
    if manifest.get("operation") != "registry-property-identity" or manifest.get("run_id") != run_id:
        raise IdentityMaintenanceError("backup_manifest_scope_mismatch")
    databases = manifest.get("databases") or []
    if not isinstance(databases, list) or len(databases) != 1:
        raise IdentityMaintenanceError("registry_backup_missing")
    entry = databases[0]
    filename = str(entry.get("filename") or "")
    if (
        entry.get("database") != settings.MYSQL_REGISTRY_DB
        or not filename
        or Path(filename).name != filename
        or not HASH_RE.fullmatch(str(entry.get("sha256") or ""))
        or int(entry.get("size_bytes") or 0) <= 0
    ):
        raise IdentityMaintenanceError("registry_backup_missing")
    backup_path = (manifest_path.parent / filename).resolve()
    if backup_path.parent != manifest_path.parent or not backup_path.is_file():
        raise IdentityMaintenanceError("registry_backup_checksum_mismatch")
    if backup_path.stat().st_size != int(entry["size_bytes"]):
        raise IdentityMaintenanceError("registry_backup_checksum_mismatch")
    if _file_sha256(backup_path) != entry["sha256"]:
        raise IdentityMaintenanceError("registry_backup_checksum_mismatch")
    return manifest


async def _require_maintenance_schema(cur) -> None:
    await cur.execute("SHOW COLUMNS FROM registry_property_identity_mappings")
    columns = {str(row[0]) for row in await cur.fetchall()}
    if not REQUIRED_MAPPING_COLUMNS.issubset(columns):
        raise IdentityMaintenanceError("identity_mapping_schema_not_ready")
    await cur.execute("SHOW COLUMNS FROM registry_property_identity_events")
    event_columns = {str(row[0]) for row in await cur.fetchall()}
    if not REQUIRED_EVENT_COLUMNS.issubset(event_columns):
        raise IdentityMaintenanceError("identity_event_schema_not_ready")
    await cur.execute("SHOW TABLES LIKE %s", (REFERENCE_EVENT_TABLE,))
    if not await cur.fetchone():
        raise IdentityMaintenanceError("identity_reference_audit_schema_not_ready")
    await cur.execute(f"SHOW COLUMNS FROM {REFERENCE_EVENT_TABLE}")
    reference_columns = {str(row[0]) for row in await cur.fetchall()}
    if not REQUIRED_REFERENCE_EVENT_COLUMNS.issubset(reference_columns):
        raise IdentityMaintenanceError("identity_reference_audit_schema_not_ready")


def _certificate_record_hash(record_id: int, source_content_hash: Any) -> str:
    value = f"registry_property_certificates\0{int(record_id)}\0{str(source_content_hash or '')}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _lock(cur) -> None:
    await cur.execute("SELECT GET_LOCK(%s, 0)", (LOCK_NAME,))
    row = await cur.fetchone()
    if not row or int(row[0]) != 1:
        raise IdentityMaintenanceError("identity_maintenance_busy")


async def _unlock(cur) -> None:
    await cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))


async def _reference_events(cur, *, run_id: str, old_id: int, for_update: bool = False) -> list[tuple]:
    suffix = " FOR UPDATE" if for_update else ""
    await cur.execute(
        f"SELECT table_name,column_name,record_id,record_hash FROM {REFERENCE_EVENT_TABLE} "
        "WHERE maintenance_run_id=%s AND old_property_id=%s ORDER BY id" + suffix,
        (run_id, old_id),
    )
    return list(await cur.fetchall())


async def _certificate_audit_matches(
    cur, *, run_id: str, old_id: int, canonical_id: int, candidate_class: str,
    expected_property_id: int, for_update: bool = False,
) -> bool:
    events = await _reference_events(cur, run_id=run_id, old_id=old_id, for_update=for_update)
    if candidate_class == "no_references_candidate":
        return not events
    if candidate_class != "certificate_only_exception_candidate" or len(events) != 1:
        return False
    table_name, column_name, record_id, record_hash = events[0]
    if (table_name, column_name) != ("registry_property_certificates", "property_id"):
        return False
    suffix = " FOR UPDATE" if for_update else ""
    await cur.execute(
        "SELECT property_id,source_content_hash FROM registry_property_certificates WHERE id=%s" + suffix,
        (record_id,),
    )
    row = await cur.fetchone()
    return bool(
        row and int(row[0]) == expected_property_id
        and _certificate_record_hash(int(record_id), row[1]) == str(record_hash)
    )


async def _mapping_events_match(cur, *, run_id: str, old_id: int, canonical_id: int,
                                candidate_class: str, actor: str, rolled_back: bool = False) -> bool:
    if not ACTOR_RE.fullmatch(actor):
        return False
    reason = (
        "single_group_apply_certificate_only"
        if candidate_class == "certificate_only_exception_candidate"
        else "single_group_apply_no_references"
    )
    await cur.execute(
        "SELECT event_type,reason_code,mapping_version,maintenance_actor FROM registry_property_identity_events "
        "WHERE maintenance_run_id=%s AND old_property_id=%s AND canonical_property_id=%s ORDER BY id",
        (run_id, old_id, canonical_id),
    )
    events = list(await cur.fetchall())
    expected = [("mapping_created", reason, 1, actor)]
    if rolled_back:
        if len(events) != 2 or not ACTOR_RE.fullmatch(str(events[1][3] or "")):
            return False
        expected.append(("mapping_rollback", "single_group_rollback", 2, str(events[1][3])))
    return [(str(row[0]), str(row[1]), int(row[2]), str(row[3])) for row in events] == expected


async def run_apply(*, run_id: str, old_property_id: int, canonical_property_id: int,
                    measurement_hash: str, backup_manifest: str, confirmation: str, actor: str) -> dict[str, Any]:
    validate_args(run_id=run_id, old_property_id=old_property_id, canonical_property_id=canonical_property_id, actor=actor)
    validate_database_scope()
    if not HASH_RE.fullmatch(measurement_hash) or confirmation != CONFIRM_APPLY:
        raise IdentityMaintenanceError("invalid_apply_confirmation")
    _verify_manifest(backup_manifest, run_id=run_id)
    conn = await open_connection(settings.MYSQL_REGISTRY_DB)
    locked = False
    try:
        async with conn.cursor() as cur:
            await _set_query_budget(cur)
        await conn.begin()
        async with conn.cursor() as cur:
            await _lock(cur)
            locked = True
            await _check_scope(cur)
            await _require_maintenance_schema(cur)
            await cur.execute(
                "SELECT old_property_id,canonical_property_id,community_id,status,maintenance_run_id,version,"
                "migration_measurement_hash,post_apply_measurement_hash,candidate_class,maintenance_actor "
                "FROM registry_property_identity_mappings WHERE old_property_id=%s FOR UPDATE",
                (old_property_id,),
            )
            mapping = await cur.fetchone()
            if mapping:
                if (
                    int(mapping[1]) == canonical_property_id and str(mapping[3]) == "active"
                    and str(mapping[4]) == run_id and str(mapping[6]) == measurement_hash
                    and str(mapping[8]) in {"no_references_candidate", "certificate_only_exception_candidate"}
                ):
                    current = await _measure_cursor(
                        cur, run_id=run_id, actor=actor, old_id=old_property_id,
                        canonical_id=canonical_property_id, for_update=True,
                    )
                    if current["measurement_hash"] != str(mapping[7]):
                        raise IdentityMaintenanceError("idempotent_apply_state_changed")
                    if int(next(item["community_id"] for item in current["properties"] if int(item["id"]) == old_property_id)) != int(mapping[2]):
                        raise IdentityMaintenanceError("idempotent_apply_mapping_community_mismatch")
                    if not await _mapping_events_match(
                        cur, run_id=run_id, old_id=old_property_id,
                        canonical_id=canonical_property_id, candidate_class=str(mapping[8]),
                        actor=str(mapping[9]),
                    ) or not await _certificate_audit_matches(
                        cur, run_id=run_id, old_id=old_property_id,
                        canonical_id=canonical_property_id, candidate_class=str(mapping[8]),
                        expected_property_id=canonical_property_id,
                    ):
                        raise IdentityMaintenanceError("idempotent_apply_audit_mismatch")
                    await conn.rollback()
                    return {"command": "migrate", "run_id": run_id, "idempotent": True,
                            "maintenance_actor": str(mapping[9]),
                            "measurement_hash": measurement_hash, "post_apply_measurement_hash": str(mapping[7])}
                raise IdentityMaintenanceError("existing_identity_mapping_conflict")
            current = await _measure_cursor(
                cur, run_id=run_id, actor=actor, old_id=old_property_id,
                canonical_id=canonical_property_id, for_update=True,
            )
            if current["measurement_hash"] != measurement_hash:
                raise IdentityMaintenanceError("measurement_changed")
            if current["disposition"] not in {"no_references_candidate", "certificate_only_exception_candidate"}:
                raise IdentityMaintenanceError("candidate_not_eligible")
            await cur.execute(
                "SELECT 1 FROM registry_property_identity_mappings WHERE canonical_property_id=%s AND status='active' FOR UPDATE",
                (old_property_id,),
            )
            if await cur.fetchone():
                raise IdentityMaintenanceError("old_property_is_canonical_target")
            await cur.execute(
                "SELECT 1 FROM registry_property_identity_mappings WHERE old_property_id=%s AND status='active' FOR UPDATE",
                (canonical_property_id,),
            )
            if await cur.fetchone():
                raise IdentityMaintenanceError("canonical_property_is_already_merged")
            properties = {int(item["id"]): item for item in current["properties"]}
            community_id = int(properties[old_property_id]["community_id"])
            if int(properties[canonical_property_id]["community_id"]) != community_id:
                raise IdentityMaintenanceError("property_community_changed")
            candidate_class = str(current["disposition"])
            await cur.execute(
                "INSERT INTO registry_property_identity_mappings "
                "(old_property_id,canonical_property_id,community_id,status,version,maintenance_run_id,"
                "migration_measurement_hash,candidate_class,maintenance_actor) "
                "VALUES (%s,%s,%s,'active',1,%s,%s,%s,%s)",
                (old_property_id, canonical_property_id, community_id, run_id, measurement_hash, candidate_class, actor),
            )
            if cur.rowcount != 1:
                raise IdentityMaintenanceError("mapping_insert_rejected")
            apply_reason = (
                "single_group_apply_certificate_only"
                if candidate_class == "certificate_only_exception_candidate"
                else "single_group_apply_no_references"
            )
            await cur.execute(
                "INSERT INTO registry_property_identity_events "
                "(old_property_id,canonical_property_id,community_id,event_type,mapping_version,maintenance_run_id,maintenance_actor,reason_code) "
                "VALUES (%s,%s,%s,'mapping_created',1,%s,%s,%s)",
                (old_property_id, canonical_property_id, community_id, run_id, actor, apply_reason),
            )
            moved = 0
            if candidate_class == "certificate_only_exception_candidate":
                await cur.execute(
                    "SELECT id,source_content_hash FROM registry_property_certificates "
                    "WHERE property_id=%s AND source_type='certificate' FOR UPDATE", (old_property_id,),
                )
                certificates = list(await cur.fetchall())
                if len(certificates) != 1:
                    raise IdentityMaintenanceError("certificate_reference_changed")
                certificate_id, content_hash = certificates[0]
                await cur.execute(
                    "SELECT id FROM registry_source_records "
                    "WHERE entity_type='property_certificate' AND entity_id=%s FOR UPDATE",
                    (certificate_id,),
                )
                source_rows = list(await cur.fetchall())
                if len(source_rows) != 1:
                    raise IdentityMaintenanceError("certificate_source_reference_changed")
                await cur.execute(
                    "UPDATE registry_property_certificates SET property_id=%s WHERE id=%s AND property_id=%s",
                    (canonical_property_id, certificate_id, old_property_id),
                )
                if cur.rowcount != 1:
                    raise IdentityMaintenanceError("certificate_move_rejected")
                await cur.execute(
                    f"INSERT INTO {REFERENCE_EVENT_TABLE} "
                    "(maintenance_run_id,old_property_id,canonical_property_id,table_name,column_name,record_id,record_hash) "
                    "VALUES (%s,%s,%s,'registry_property_certificates','property_id',%s,%s)",
                    (run_id, old_property_id, canonical_property_id, int(certificate_id),
                     _certificate_record_hash(int(certificate_id), content_hash)),
                )
                moved = 1
            post_apply = await _measure_cursor(
                cur, run_id=run_id, actor=actor, old_id=old_property_id,
                canonical_id=canonical_property_id, for_update=True,
            )
            if post_apply["reference_summary"]["blocking_reference_count"] != 0:
                raise IdentityMaintenanceError("post_apply_active_reference_remains")
            await cur.execute(
                "UPDATE registry_property_identity_mappings SET post_apply_measurement_hash=%s "
                "WHERE old_property_id=%s AND status='active' AND maintenance_run_id=%s",
                (post_apply["measurement_hash"], old_property_id, run_id),
            )
            if cur.rowcount != 1:
                raise IdentityMaintenanceError("mapping_snapshot_update_rejected")
        await conn.commit()
        return {"command": "migrate", "run_id": run_id, "old_property_id": old_property_id,
                "maintenance_actor": actor,
                "canonical_property_id": canonical_property_id, "measurement_hash": measurement_hash,
                "post_apply_measurement_hash": post_apply["measurement_hash"],
                "certificate_references_moved": moved, "idempotent": False}
    except BaseException:
        await conn.rollback()
        raise
    finally:
        if locked:
            try:
                async with conn.cursor() as cur:
                    await _unlock(cur)
            except Exception:
                pass
        await close_connection(conn)


async def run_verify(*, run_id: str, old_property_id: int, canonical_property_id: int, actor: str) -> dict[str, Any]:
    validate_args(run_id=run_id, old_property_id=old_property_id, canonical_property_id=canonical_property_id, actor=actor)
    validate_database_scope()
    conn = await open_connection(settings.MYSQL_REGISTRY_DB)
    try:
        async with conn.cursor() as cur:
            await _set_query_budget(cur)
            await cur.execute("SET SESSION TRANSACTION READ ONLY")
            await cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            await _check_scope(cur)
            await _require_maintenance_schema(cur)
            await cur.execute(
                "SELECT canonical_property_id,community_id,status,maintenance_run_id,version,migration_measurement_hash,"
                "post_apply_measurement_hash,post_rollback_measurement_hash,candidate_class,maintenance_actor "
                "FROM registry_property_identity_mappings "
                "WHERE old_property_id=%s", (old_property_id,),
            )
            mapping = await cur.fetchone()
            if not mapping or int(mapping[0]) != canonical_property_id or str(mapping[3]) != run_id:
                return {"command": "verify", "run_id": run_id, "checks": {"mapping_matches_run": False}, "ok": False}
            current = await _measure_cursor(
                cur, run_id=run_id, actor=actor, old_id=old_property_id, canonical_id=canonical_property_id,
            )
            candidate_class = str(mapping[8])
            mapping_actor = str(mapping[9])
            mapping_actor_valid = bool(ACTOR_RE.fullmatch(mapping_actor))
            rolled_back = str(mapping[2]) == "revoked"
            expected_state_hash = str(mapping[7] if rolled_back else mapping[6])
            reference_summary = current["reference_summary"]
            expected_property_id = old_property_id if rolled_back else canonical_property_id
            measured_community_id = next(
                int(item["community_id"]) for item in current["properties"] if int(item["id"]) == old_property_id
            )
            checks = {
                "mapping_state_valid": (str(mapping[2]) == "active" and int(mapping[4]) == 1)
                or (str(mapping[2]) == "revoked" and int(mapping[4]) == 2),
                "mapping_actor_valid": mapping_actor_valid,
                "mapping_community_matches": int(mapping[1]) == measured_community_id,
                "measurement_unchanged": bool(expected_state_hash and current["measurement_hash"] == expected_state_hash),
                "reference_inventory_clear": (
                    current["disposition"] == candidate_class
                    if rolled_back
                    else int(reference_summary["blocking_reference_count"]) == 0
                    and int(reference_summary["old_certificate_count"]) == 0
                    and int(reference_summary["old_certificate_source_count"]) == 0
                    and int(reference_summary["old_small_community_link_count"]) == 0
                ),
                "certificate_count_matches_class": (
                    (
                        int(reference_summary["old_certificate_count"]) == 1
                        and int(reference_summary["old_certificate_source_count"]) == 1
                        and int(reference_summary["canonical_certificate_count"]) == 0
                        and int(reference_summary["canonical_certificate_source_count"]) == 0
                    ) if rolled_back and candidate_class == "certificate_only_exception_candidate"
                    else (
                        int(reference_summary["old_certificate_count"]) == 0
                        and int(reference_summary["old_certificate_source_count"]) == 0
                        and int(reference_summary["canonical_certificate_count"]) == 1
                        and int(reference_summary["canonical_certificate_source_count"]) == 1
                    ) if not rolled_back and candidate_class == "certificate_only_exception_candidate"
                    else int(reference_summary["old_certificate_count"]) == 0
                    and int(reference_summary["old_certificate_source_count"]) == 0
                ),
                "certificate_reference_audit_matches": await _certificate_audit_matches(
                    cur, run_id=run_id, old_id=old_property_id, canonical_id=canonical_property_id,
                    candidate_class=candidate_class, expected_property_id=expected_property_id,
                ),
                "mapping_events_match": await _mapping_events_match(
                    cur, run_id=run_id, old_id=old_property_id, canonical_id=canonical_property_id,
                    candidate_class=candidate_class, actor=mapping_actor, rolled_back=rolled_back,
                ),
            }
            return {"command": "verify", "run_id": run_id, "old_property_id": old_property_id,
                    "maintenance_actor": mapping_actor,
                    "canonical_property_id": canonical_property_id, "state": "rolled_back" if rolled_back else "active",
                    "measurement_hash": current["measurement_hash"], "checks": checks, "ok": all(checks.values())}
    finally:
        try:
            await conn.rollback()
        finally:
            await close_connection(conn)


async def run_rollback(*, run_id: str, old_property_id: int, canonical_property_id: int,
                       measurement_hash: str, backup_manifest: str, confirmation: str, actor: str) -> dict[str, Any]:
    validate_args(run_id=run_id, old_property_id=old_property_id, canonical_property_id=canonical_property_id, actor=actor)
    validate_database_scope()
    if not HASH_RE.fullmatch(measurement_hash) or confirmation != CONFIRM_ROLLBACK:
        raise IdentityMaintenanceError("invalid_rollback_confirmation")
    _verify_manifest(backup_manifest, run_id=run_id)
    conn = await open_connection(settings.MYSQL_REGISTRY_DB)
    locked = False
    try:
        async with conn.cursor() as cur:
            await _set_query_budget(cur)
        await conn.begin()
        async with conn.cursor() as cur:
            await _lock(cur)
            locked = True
            await _check_scope(cur)
            await _require_maintenance_schema(cur)
            await cur.execute(
                "SELECT canonical_property_id,community_id,status,maintenance_run_id,version,migration_measurement_hash,"
                "post_apply_measurement_hash,post_rollback_measurement_hash,candidate_class,maintenance_actor FROM registry_property_identity_mappings "
                "WHERE old_property_id=%s FOR UPDATE", (old_property_id,),
            )
            mapping = await cur.fetchone()
            if not mapping or int(mapping[0]) != canonical_property_id or str(mapping[3]) != run_id:
                raise IdentityMaintenanceError("rollback_mapping_mismatch")
            candidate_class = str(mapping[8])
            if str(mapping[2]) == "revoked":
                current = await _measure_cursor(
                    cur, run_id=run_id, actor=actor, old_id=old_property_id,
                    canonical_id=canonical_property_id, for_update=True,
                )
                if (
                    str(mapping[7]) == measurement_hash and current["measurement_hash"] == measurement_hash
                    and await _mapping_events_match(
                        cur, run_id=run_id, old_id=old_property_id,
                            canonical_id=canonical_property_id, candidate_class=candidate_class,
                            actor=str(mapping[9]), rolled_back=True,
                    ) and await _certificate_audit_matches(
                        cur, run_id=run_id, old_id=old_property_id,
                        canonical_id=canonical_property_id, candidate_class=candidate_class,
                        expected_property_id=old_property_id, for_update=True,
                    )
                ):
                    await conn.rollback()
                    return {"command": "rollback", "run_id": run_id, "idempotent": True,
                            "maintenance_actor": str(mapping[9]),
                            "measurement_hash": measurement_hash, "certificate_references_restored": int(candidate_class == "certificate_only_exception_candidate")}
                raise IdentityMaintenanceError("rollback_already_completed_state_changed")
            if str(mapping[2]) != "active" or int(mapping[4]) != 1:
                raise IdentityMaintenanceError("rollback_mapping_mismatch")
            if str(mapping[6]) != measurement_hash:
                raise IdentityMaintenanceError("rollback_measurement_mismatch")
            current = await _measure_cursor(
                cur, run_id=run_id, actor=actor, old_id=old_property_id,
                canonical_id=canonical_property_id, for_update=True,
            )
            if current["measurement_hash"] != measurement_hash:
                raise IdentityMaintenanceError("rollback_state_changed")
            measured_community_id = next(
                int(item["community_id"]) for item in current["properties"] if int(item["id"]) == old_property_id
            )
            if int(mapping[1]) != measured_community_id:
                raise IdentityMaintenanceError("rollback_mapping_community_mismatch")
            if int(current["reference_summary"]["blocking_reference_count"]) != 0:
                raise IdentityMaintenanceError("rollback_new_reference_present")
            if int(current["reference_summary"]["old_certificate_count"]) != 0 or int(current["reference_summary"]["old_certificate_source_count"]) != 0:
                raise IdentityMaintenanceError("rollback_old_certificate_reference_present")
            if candidate_class == "certificate_only_exception_candidate" and (
                int(current["reference_summary"]["canonical_certificate_count"]) != 1
                or int(current["reference_summary"]["canonical_certificate_source_count"]) != 1
            ):
                raise IdentityMaintenanceError("rollback_canonical_certificate_set_changed")
            if not await _mapping_events_match(
                cur, run_id=run_id, old_id=old_property_id,
                canonical_id=canonical_property_id, candidate_class=candidate_class,
                actor=str(mapping[9]),
            ) or not await _certificate_audit_matches(
                cur, run_id=run_id, old_id=old_property_id,
                canonical_id=canonical_property_id, candidate_class=candidate_class,
                expected_property_id=canonical_property_id, for_update=True,
            ):
                raise IdentityMaintenanceError("rollback_apply_audit_mismatch")
            events = await _reference_events(cur, run_id=run_id, old_id=old_property_id, for_update=True)
            expected_count = int(candidate_class == "certificate_only_exception_candidate")
            if len(events) != expected_count:
                raise IdentityMaintenanceError("rollback_reference_audit_mismatch")
            restored = 0
            for table_name, column_name, record_id, record_hash in events:
                if table_name != "registry_property_certificates" or column_name != "property_id":
                    raise IdentityMaintenanceError("rollback_reference_type_unsupported")
                await cur.execute(
                    "SELECT property_id,source_content_hash FROM registry_property_certificates WHERE id=%s FOR UPDATE",
                    (record_id,),
                )
                row = await cur.fetchone()
                if not row or int(row[0]) != canonical_property_id or _certificate_record_hash(int(record_id), row[1]) != str(record_hash):
                    raise IdentityMaintenanceError("rollback_reference_changed")
                await cur.execute(
                    "SELECT COUNT(*) FROM registry_property_certificates WHERE property_id=%s AND source_type='certificate'",
                    (canonical_property_id,),
                )
                if int((await cur.fetchone())[0] or 0) != 1:
                    raise IdentityMaintenanceError("rollback_canonical_certificate_set_changed")
                await cur.execute(
                    "UPDATE registry_property_certificates SET property_id=%s WHERE id=%s AND property_id=%s",
                    (old_property_id, record_id, canonical_property_id),
                )
                if cur.rowcount != 1:
                    raise IdentityMaintenanceError("rollback_reference_restore_rejected")
                restored += 1
            await cur.execute(
                "UPDATE registry_property_identity_mappings SET status='revoked',version=version+1 "
                "WHERE old_property_id=%s AND status='active' AND maintenance_run_id=%s",
                (old_property_id, run_id),
            )
            if cur.rowcount != 1:
                raise IdentityMaintenanceError("rollback_mapping_update_rejected")
            await cur.execute(
                "INSERT INTO registry_property_identity_events "
                "(old_property_id,canonical_property_id,community_id,event_type,mapping_version,maintenance_run_id,maintenance_actor,reason_code) "
                "SELECT old_property_id,canonical_property_id,community_id,'mapping_rollback',version,maintenance_run_id,%s,'single_group_rollback' "
                "FROM registry_property_identity_mappings WHERE old_property_id=%s AND maintenance_run_id=%s",
                (actor, old_property_id, run_id),
            )
            post_rollback = await _measure_cursor(
                cur, run_id=run_id, actor=actor, old_id=old_property_id,
                canonical_id=canonical_property_id, for_update=True,
            )
            if post_rollback["measurement_hash"] != str(mapping[5]):
                raise IdentityMaintenanceError("rollback_did_not_restore_measured_state")
            await cur.execute(
                "UPDATE registry_property_identity_mappings SET post_rollback_measurement_hash=%s "
                "WHERE old_property_id=%s AND status='revoked' AND maintenance_run_id=%s",
                (post_rollback["measurement_hash"], old_property_id, run_id),
            )
            if cur.rowcount != 1:
                raise IdentityMaintenanceError("rollback_snapshot_update_rejected")
        await conn.commit()
        return {"command": "rollback", "run_id": run_id, "old_property_id": old_property_id,
                "maintenance_actor": actor,
                "canonical_property_id": canonical_property_id,
                "measurement_hash": post_rollback["measurement_hash"],
                "certificate_references_restored": restored, "idempotent": False}
    except BaseException:
        await conn.rollback()
        raise
    finally:
        if locked:
            try:
                async with conn.cursor() as cur:
                    await _unlock(cur)
            except Exception:
                pass
        await close_connection(conn)


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("measure", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--environment", required=True, choices=("production", "staging", "development"))
        command.add_argument("--run-id", required=True)
        command.add_argument("--old-property-id", required=True, type=int)
        command.add_argument("--canonical-property-id", required=True, type=int)
        command.add_argument("--actor", required=True)
    apply = sub.add_parser("migrate")
    apply.add_argument("--apply", action="store_true")
    apply.add_argument("--environment", required=True, choices=("production", "staging", "development"))
    apply.add_argument("--run-id", required=True)
    apply.add_argument("--old-property-id", required=True, type=int)
    apply.add_argument("--canonical-property-id", required=True, type=int)
    apply.add_argument("--actor", required=True)
    apply.add_argument("--measurement-hash", required=True)
    apply.add_argument("--backup-manifest", required=True)
    apply.add_argument("--confirm", required=True)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--environment", required=True, choices=("production", "staging", "development"))
    rollback.add_argument("--run-id", required=True)
    rollback.add_argument("--old-property-id", required=True, type=int)
    rollback.add_argument("--canonical-property-id", required=True, type=int)
    rollback.add_argument("--actor", required=True)
    rollback.add_argument("--measurement-hash", required=True)
    rollback.add_argument("--backup-manifest", required=True)
    rollback.add_argument("--confirm", required=True)
    return parser


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    if args.environment != settings.APP_ENVIRONMENT:
        raise IdentityMaintenanceError("environment_identity_mismatch")
    if args.command == "measure":
        return await run_measure(
            run_id=args.run_id, old_property_id=args.old_property_id,
            canonical_property_id=args.canonical_property_id, actor=args.actor,
        )
    if args.command == "verify":
        return await run_verify(
            run_id=args.run_id, old_property_id=args.old_property_id,
            canonical_property_id=args.canonical_property_id, actor=args.actor,
        )
    if args.command == "rollback":
        return await run_rollback(
            run_id=args.run_id, old_property_id=args.old_property_id,
            canonical_property_id=args.canonical_property_id,
            measurement_hash=args.measurement_hash, backup_manifest=args.backup_manifest,
            confirmation=args.confirm, actor=args.actor,
        )
    if not args.apply:
        raise IdentityMaintenanceError("apply_required")
    return await run_apply(run_id=args.run_id, old_property_id=args.old_property_id, canonical_property_id=args.canonical_property_id,
                           measurement_hash=args.measurement_hash, backup_manifest=args.backup_manifest,
                           confirmation=args.confirm, actor=args.actor)


if __name__ == "__main__":
    parsed = parser().parse_args()
    try:
        result = asyncio.run(main_async(parsed))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, IdentityMaintenanceError) else "unexpected_maintenance_failure"
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "reason": reason}, ensure_ascii=False))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if parsed.command == "verify" and not result.get("ok", False):
        raise SystemExit(1)
