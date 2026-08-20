"""人员标签名单的只读预览和幂等入库。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from deps import require_permission
from routers.registry import get_registry_db
from services.audit import record_admin_audit, request_audit_fields
from services.permissions import REGISTRY_IMPORT_MANAGE
from services.registry_security import hmac_digest
from services.watch_import import (
    CORE_CATEGORY_CODES,
    WatchImportRow,
    parse_watch_workbook,
    summarize_watch_rows,
)


router = APIRouter(prefix="/api/registry", tags=["人员标签"])
MAX_WATCH_IMPORT_BYTES = 50 * 1024 * 1024
WRITE_CHUNK = 500


def _json(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _row_from_payload(payload: dict) -> WatchImportRow:
    fields = WatchImportRow.__dataclass_fields__
    return WatchImportRow(**{key: payload.get(key, "") for key in fields})


def _chunks(values: list, size: int = WRITE_CHUNK):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


async def _load_batch_rows(cur, batch_id: int) -> tuple[list[tuple[int, WatchImportRow]], str]:
    await cur.execute(
        "SELECT id, payload_json FROM registry_source_records "
        "WHERE batch_id=%s AND entity_type='watch_tag_import' ORDER BY id",
        (batch_id,),
    )
    rows = [(int(item[0]), _row_from_payload(_json(item[1]))) for item in await cur.fetchall()]
    category_codes = {item.category_code for _, item in rows}
    if not rows or len(category_codes) != 1:
        raise HTTPException(409, "人员标签导入批次内容不完整")
    category_code = next(iter(category_codes))
    if category_code not in CORE_CATEGORY_CODES:
        raise HTTPException(409, "人员标签导入分类已失效")
    return rows, category_code


async def _existing_preview(cur, summary: dict, category_code: str) -> dict:
    await cur.execute(
        "SELECT id FROM watch_categories WHERE code=%s AND is_active=1",
        (category_code,),
    )
    category = await cur.fetchone()
    if not category:
        raise HTTPException(409, "人员标签分类不存在或已停用")
    category_id = int(category[0])
    people = summary["people"]
    digest_to_item: dict[str, dict] = {}
    for item in people:
        digest, _ = hmac_digest(item["identity_number"], kind="identity")
        if digest:
            digest_to_item[digest] = item
    existing: dict[str, tuple[int, str, str]] = {}
    for chunk in _chunks(list(digest_to_item)):
        placeholders = ",".join(["%s"] * len(chunk))
        await cur.execute(
            "SELECT id, name, identity_hmac, status FROM watch_people "
            f"WHERE identity_hmac IN ({placeholders})",
            tuple(chunk),
        )
        for person_id, name, digest, status in await cur.fetchall():
            existing[str(digest)] = (int(person_id), str(name), str(status))
    existing_name_conflicts = 0
    inactive_people = 0
    for digest, (_, name, status) in existing.items():
        item = digest_to_item[digest]
        if status != "active":
            inactive_people += 1
        elif name.strip() != str(item["name"]).strip():
            existing_name_conflicts += 1
    active_person_ids = [item[0] for item in existing.values() if item[2] == "active"]
    existing_assignments = 0
    for chunk in _chunks(active_person_ids):
        if not chunk:
            continue
        placeholders = ",".join(["%s"] * len(chunk))
        await cur.execute(
            "SELECT COUNT(DISTINCT person_id) FROM watch_assignments "
            f"WHERE person_id IN ({placeholders}) AND category_id=%s "
            "AND status='active' AND valid_from<=UTC_TIMESTAMP() "
            "AND (valid_to IS NULL OR valid_to>=UTC_TIMESTAMP()) "
            "AND (released_at IS NULL OR released_at>UTC_TIMESTAMP())",
            tuple(chunk) + (category_id,),
        )
        existing_assignments += int((await cur.fetchone())[0])
    existing_people = len(existing)
    blocking_count = int(summary["blocking_count"]) + existing_name_conflicts + inactive_people
    return {
        **{key: value for key, value in summary.items() if key != "people"},
        "category_code": category_code,
        "existing_people": existing_people,
        "new_people": len(people) - existing_people,
        "existing_assignments": existing_assignments,
        "new_assignments": max(
            0,
            len(people) - existing_assignments - existing_name_conflicts - inactive_people,
        ),
        "existing_name_conflict_count": existing_name_conflicts,
        "inactive_people_count": inactive_people,
        "blocking_count": blocking_count,
        "can_confirm": blocking_count == 0,
    }


def _require_super_admin(user: dict) -> None:
    if user.get("role") != "super_admin":
        raise HTTPException(403, "只有超级管理员可以批量导入人员标签")


@router.post("/watch/imports/preview")
async def preview_watch_import(
    request: Request,
    files: list[UploadFile] = File(...),
    category_code: str = Form("通勤人员"),
    user: dict = Depends(require_permission(REGISTRY_IMPORT_MANAGE)),
    conn=Depends(get_registry_db),
):
    _require_super_admin(user)
    if category_code not in CORE_CATEGORY_CODES:
        raise HTTPException(422, "人员标签分类无效")
    if not files:
        raise HTTPException(422, "请选择人员名单文件")
    contents: list[tuple[str, bytes]] = []
    total_bytes = 0
    for file in files:
        filename = str(file.filename or "人员名单")
        content = await file.read()
        total_bytes += len(content)
        if total_bytes > MAX_WATCH_IMPORT_BYTES:
            raise HTTPException(413, "人员名单合计不能超过 50MB")
        contents.append((filename, content))
    rows: list[WatchImportRow] = []
    digest = hashlib.sha256(category_code.encode("utf-8"))
    content_hashes: list[str] = []
    for filename, content in contents:
        content_hashes.append(hashlib.sha256(content).hexdigest())
        try:
            rows.extend(parse_watch_workbook(content, filename, category_code=category_code))
        except ValueError as exc:
            raise HTTPException(422, f"{filename}：{exc}") from exc
    for content_hash in sorted(content_hashes):
        digest.update(content_hash.encode("ascii"))
    summary = summarize_watch_rows(rows)
    file_hash = digest.hexdigest()
    display_name = "、".join(filename for filename, _ in contents)[:255]
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, status FROM registry_source_batches "
                "WHERE source_type='watch_tag' AND file_sha256=%s",
                (file_hash,),
            )
            existing = await cur.fetchone()
            if existing:
                batch_id = int(existing[0])
                stored_rows, stored_category = await _load_batch_rows(cur, batch_id)
                result = await _existing_preview(
                    cur,
                    summarize_watch_rows([item for _, item in stored_rows]),
                    stored_category,
                )
                result.update({
                    "batch_id": batch_id,
                    "status": str(existing[1]),
                    "idempotent": True,
                    "file_count": len(contents),
                })
                await conn.rollback()
                return result
            await cur.execute(
                "INSERT INTO registry_source_batches "
                "(source_type, file_name, file_sha256, status, imported_count, candidate_count, conflict_count, created_by) "
                "VALUES ('watch_tag',%s,%s,'preview',0,%s,%s,%s)",
                (display_name, file_hash, summary["unique_people"], summary["blocking_count"], user["id"]),
            )
            batch_id = int(cur.lastrowid)
            for chunk in _chunks(rows):
                await cur.executemany(
                    "INSERT INTO registry_source_records "
                    "(batch_id, source_ref, entity_type, payload_json) VALUES (%s,%s,'watch_tag_import',%s)",
                    [
                        (batch_id, item.source_ref, json.dumps(item.payload(), ensure_ascii=False))
                        for item in chunk
                    ],
                )
            result = await _existing_preview(cur, summary, category_code)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    result.update({
        "batch_id": batch_id,
        "status": "preview",
        "idempotent": False,
        "file_count": len(contents),
    })
    await record_admin_audit(
        user,
        "registry.watch_import.preview",
        target_type="registry_source_batch",
        target_name=str(batch_id),
        detail={
            "file_count": len(contents),
            "total_rows": result["total_rows"],
            "unique_people": result["unique_people"],
            "blocking_count": result["blocking_count"],
        },
        **request_audit_fields(request),
    )
    return result


@router.post("/watch/imports/{batch_id}/confirm")
async def confirm_watch_import(
    batch_id: int,
    request: Request,
    user: dict = Depends(require_permission(REGISTRY_IMPORT_MANAGE)),
    conn=Depends(get_registry_db),
):
    _require_super_admin(user)
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM registry_source_batches "
                "WHERE id=%s AND source_type='watch_tag' FOR UPDATE",
                (batch_id,),
            )
            batch = await cur.fetchone()
            if not batch:
                raise HTTPException(404, "人员标签导入批次不存在")
            if str(batch[0]) == "imported":
                await conn.rollback()
                return {"batch_id": batch_id, "status": "imported", "idempotent": True}
            records, category_code = await _load_batch_rows(cur, batch_id)
            summary = summarize_watch_rows([item for _, item in records])
            preview = await _existing_preview(cur, summary, category_code)
            if not preview["can_confirm"]:
                raise HTTPException(409, "名单仍有阻断问题，请先核对预览统计")
            await cur.execute(
                "SELECT id FROM watch_categories WHERE code=%s AND is_active=1 FOR UPDATE",
                (category_code,),
            )
            category_id = int((await cur.fetchone())[0])
            people = summary["people"]
            digest_to_item: dict[str, dict] = {}
            for item in people:
                digest, version = hmac_digest(item["identity_number"], kind="identity")
                if digest:
                    item["identity_hmac"] = digest
                    item["hmac_version"] = version
                    digest_to_item[digest] = item
            existing: dict[str, tuple[int, str]] = {}
            for chunk in _chunks(list(digest_to_item)):
                placeholders = ",".join(["%s"] * len(chunk))
                await cur.execute(
                    "SELECT id, name, identity_hmac FROM watch_people "
                    f"WHERE identity_hmac IN ({placeholders}) AND status='active' FOR UPDATE",
                    tuple(chunk),
                )
                for person_id, name, digest in await cur.fetchall():
                    existing[str(digest)] = (int(person_id), str(name))
            new_items = [item for digest, item in digest_to_item.items() if digest not in existing]
            for chunk in _chunks(new_items):
                await cur.executemany(
                    "INSERT INTO watch_people "
                    "(name, identity_number, identity_hmac, identity_hmac_version, is_temporary, verification_status, "
                    "source_type, source_ref, created_by, updated_by) "
                    "VALUES (%s,%s,%s,%s,0,'verified','watch_import',%s,%s,%s)",
                    [
                        (
                            item["name"], item["identity_number"], item["identity_hmac"], item["hmac_version"],
                            f"batch:{batch_id}", user["id"], user["id"],
                        )
                        for item in chunk
                    ],
                )
            for chunk in _chunks(list(digest_to_item)):
                placeholders = ",".join(["%s"] * len(chunk))
                await cur.execute(
                    "SELECT id, name, identity_hmac FROM watch_people "
                    f"WHERE identity_hmac IN ({placeholders}) AND status='active'",
                    tuple(chunk),
                )
                for person_id, name, digest in await cur.fetchall():
                    existing[str(digest)] = (int(person_id), str(name))
            person_by_identity = {
                item["identity_number"]: existing[digest][0]
                for digest, item in digest_to_item.items()
            }

            phone_rows = []
            for item in people:
                if not item["phone"]:
                    continue
                phone_digest, phone_version = hmac_digest(item["phone"], kind="phone")
                phone_rows.append((
                    person_by_identity[item["identity_number"]], item["phone"], phone_digest, phone_version,
                    f"batch:{batch_id}", user["id"],
                ))
            created_phones = 0
            for person_id, phone, phone_digest, phone_version, source_ref, actor_id in phone_rows:
                await cur.execute(
                    "SELECT 1 FROM watch_person_phones WHERE person_id=%s AND phone_hmac=%s AND valid_to IS NULL LIMIT 1",
                    (person_id, phone_digest),
                )
                if await cur.fetchone():
                    continue
                await cur.execute(
                    "SELECT 1 FROM watch_person_phones WHERE person_id=%s AND valid_to IS NULL LIMIT 1",
                    (person_id,),
                )
                is_primary = 0 if await cur.fetchone() else 1
                await cur.execute(
                    "INSERT INTO watch_person_phones "
                    "(person_id, phone, phone_hmac, hmac_version, is_primary, source_type, source_ref, created_by) "
                    "VALUES (%s,%s,%s,%s,%s,'watch_import',%s,%s)",
                    (person_id, phone, phone_digest, phone_version, is_primary, source_ref, actor_id),
                )
                created_phones += 1

            person_ids = list(person_by_identity.values())
            existing_assignments: set[int] = set()
            for chunk in _chunks(person_ids):
                placeholders = ",".join(["%s"] * len(chunk))
                await cur.execute(
                    "SELECT person_id FROM watch_assignments "
                    f"WHERE person_id IN ({placeholders}) AND category_id=%s "
                    "AND status='active' AND valid_from<=UTC_TIMESTAMP() "
                    "AND (valid_to IS NULL OR valid_to>=UTC_TIMESTAMP()) "
                    "AND (released_at IS NULL OR released_at>UTC_TIMESTAMP()) FOR UPDATE",
                    tuple(chunk) + (category_id,),
                )
                existing_assignments.update(int(item[0]) for item in await cur.fetchall())
            created_assignments = 0
            now = datetime.utcnow()
            for item in people:
                person_id = person_by_identity[item["identity_number"]]
                if person_id in existing_assignments:
                    continue
                valid_from = now
                if item.get("valid_from"):
                    try:
                        valid_from = datetime.fromisoformat(str(item["valid_from"]))
                    except ValueError:
                        valid_from = now
                await cur.execute(
                    "INSERT INTO watch_assignments "
                    "(person_id, category_id, valid_from, source_type, source_ref, basis, created_by, updated_by) "
                    "VALUES (%s,%s,%s,'watch_import',%s,%s,%s,%s)",
                    (
                        person_id, category_id, valid_from, f"batch:{batch_id}",
                        f"人员标签名单批量导入；来源记录 {item['row_count']} 条",
                        user["id"], user["id"],
                    ),
                )
                assignment_id = int(cur.lastrowid)
                await cur.execute(
                    "INSERT INTO watch_assignment_versions "
                    "(assignment_id, version_no, snapshot_json, changed_by) VALUES (%s,1,%s,%s)",
                    (assignment_id, "{}", user["id"]),
                )
                created_assignments += 1

            source_links = []
            for record_id, row in records:
                person_id = person_by_identity.get(row.identity_number)
                if person_id:
                    source_links.append((person_id, record_id))
            for chunk in _chunks(source_links):
                await cur.executemany(
                    "UPDATE registry_source_records SET entity_id=%s WHERE id=%s",
                    chunk,
                )
            await cur.execute(
                "UPDATE registry_source_batches SET status='imported', imported_count=%s, conflict_count=0 WHERE id=%s",
                (len(people), batch_id),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    result = {
        "batch_id": batch_id,
        "status": "imported",
        "idempotent": False,
        "unique_people": len(people),
        "created_people": len(new_items),
        "reused_people": len(people) - len(new_items),
        "created_assignments": created_assignments,
        "existing_assignments": len(people) - created_assignments,
        "created_phones": created_phones,
    }
    await record_admin_audit(
        user,
        "registry.watch_import.confirm",
        target_type="registry_source_batch",
        target_name=str(batch_id),
        detail={key: result[key] for key in (
            "unique_people", "created_people", "reused_people",
            "created_assignments", "existing_assignments", "created_phones",
        )},
        **request_audit_fields(request),
    )
    return result
