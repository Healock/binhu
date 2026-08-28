"""辖区自购自住人员资产资料。

资料导入时校验身份证并保存 HMAC，匹配当前模型三任务后将空白/无法核实
结果更新为“近期返吴”。不调用全民防写接口，也不把原始名单行或身份证
明文写入新增表。表名和接口路径保留 qmf/self-owned 历史命名，仅用于兼容。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook

from services.qmf_registration import valid_identity, normalize_identity
from services.registry_security import hmac_digest
from services.online_source import source_row_hash, rebuild_projection
from services.parsers import get_parser


MODEL_THREE_PARSER = "疑似未注销模型三"
RULE_VERSION = "self-owned-v1"
MAX_ZIP_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_FILES = 30
MAX_ROWS = 150_000


class SelfOwnedImportError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedSelfOwned:
    file_sha256: str
    total_rows: int
    valid_rows: int
    invalid_rows: int
    duplicate_rows: int
    identities: tuple[tuple[str, int], ...]
    workbook_count: int
    names: tuple[tuple[str, str], ...] = ()


def _text(value: Any) -> str:
    return str(value or "").lstrip("\ufeff").strip()


def _header_index(headers: list[Any], *names: str) -> int | None:
    normalized = {_text(value): index for index, value in enumerate(headers)}
    for name in names:
        if name in normalized:
            return normalized[name]
    return None


def parse_self_owned_zip(content: bytes) -> ParsedSelfOwned:
    if not content:
        raise SelfOwnedImportError("自购自住名单文件为空")
    if len(content) > MAX_ZIP_BYTES:
        raise SelfOwnedImportError("自购自住名单 ZIP 超过 100MB")

    identities: dict[str, int] = {}
    names: dict[str, str] = {}
    total_rows = invalid_rows = duplicate_rows = 0
    workbook_count = 0
    try:
        archive = ZipFile(BytesIO(content))
    except BadZipFile as exc:
        raise SelfOwnedImportError("自购自住名单必须是有效 ZIP 文件") from exc
    with archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        xlsx_members = [
            item for item in members
            if PurePosixPath(item.filename).suffix.lower() == ".xlsx"
        ]
        if not xlsx_members:
            raise SelfOwnedImportError("ZIP 中没有 XLSX 文件")
        if len(xlsx_members) > MAX_FILES:
            raise SelfOwnedImportError("ZIP 中 XLSX 文件数量超过保护阈值")
        if sum(item.file_size for item in xlsx_members) > MAX_UNCOMPRESSED_BYTES:
            raise SelfOwnedImportError("ZIP 解压后的 XLSX 总大小超过保护阈值")
        for member in xlsx_members:
            workbook_count += 1
            try:
                workbook = load_workbook(
                    filename=BytesIO(archive.read(member)),
                    read_only=True,
                    data_only=True,
                )
            except Exception as exc:  # noqa: BLE001 - hide workbook details
                raise SelfOwnedImportError("名单中存在无法读取的 XLSX 文件") from exc
            try:
                for sheet in workbook.worksheets:
                    rows = sheet.iter_rows(values_only=True)
                    headers = list(next(rows, ()))
                    if not any(_text(value) for value in headers):
                        continue
                    identity_index = _header_index(headers, "居民证号", "身份证号", "身份证号码")
                    if identity_index is None:
                        raise SelfOwnedImportError("XLSX 缺少“居民证号”列")
                    for row in rows:
                        if not any(value not in (None, "") for value in row):
                            continue
                        total_rows += 1
                        if total_rows > MAX_ROWS:
                            raise SelfOwnedImportError("名单记录数超过 150000 条保护阈值")
                        identity = normalize_identity(row[identity_index] if identity_index < len(row) else "")
                        if not valid_identity(identity):
                            invalid_rows += 1
                            continue
                        digest, version = hmac_digest(identity, kind="identity")
                        if not digest:
                            invalid_rows += 1
                            continue
                        if digest in identities:
                            duplicate_rows += 1
                            continue
                        identities[digest] = version
                        name_index = _header_index(headers, "姓名", "居民姓名", "人员姓名")
                        if name_index is not None and name_index < len(row):
                            name = _text(row[name_index])[:100]
                            if name:
                                names[digest] = name
            finally:
                workbook.close()
    if not identities:
        raise SelfOwnedImportError("名单中没有有效身份证记录")
    return ParsedSelfOwned(
        file_sha256=hashlib.sha256(content).hexdigest(),
        total_rows=total_rows,
        valid_rows=len(identities),
        invalid_rows=invalid_rows,
        duplicate_rows=duplicate_rows,
        identities=tuple(sorted(identities.items())),
        names=tuple(sorted(names.items())),
        workbook_count=workbook_count,
    )


async def ensure_self_owned_schema(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _qmf_self_owned_batches (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            file_name VARCHAR(255) NOT NULL DEFAULT '',
            file_sha256 CHAR(64) NOT NULL,
            rule_version VARCHAR(40) NOT NULL DEFAULT 'self-owned-v1',
            status VARCHAR(20) NOT NULL DEFAULT 'completed',
            workbook_count INT NOT NULL DEFAULT 0,
            total_rows INT NOT NULL DEFAULT 0,
            valid_rows INT NOT NULL DEFAULT 0,
            invalid_rows INT NOT NULL DEFAULT 0,
            duplicate_rows INT NOT NULL DEFAULT 0,
            matched_tasks INT NOT NULL DEFAULT 0,
            updated_tasks INT NOT NULL DEFAULT 0,
            skipped_tasks INT NOT NULL DEFAULT 0,
            created_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME DEFAULT NULL,
            error_message VARCHAR(500) NOT NULL DEFAULT '',
            UNIQUE KEY uk_qmf_self_owned_sha (file_sha256),
            INDEX idx_qmf_self_owned_created (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _qmf_self_owned_identities (
            batch_id BIGINT NOT NULL,
            identity_hmac CHAR(64) NOT NULL,
            identity_hmac_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (batch_id, identity_hmac),
            INDEX idx_qmf_self_owned_identity (identity_hmac, batch_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)


def _public_batch(row: tuple | None) -> dict[str, Any] | None:
    if not row:
        return None
    keys = (
        "id", "file_name", "file_sha256", "rule_version", "status", "workbook_count",
        "total_rows", "valid_rows", "invalid_rows", "duplicate_rows", "matched_tasks",
        "updated_tasks", "skipped_tasks", "created_by", "created_at", "completed_at",
        "error_message",
    )
    data = dict(zip(keys, row))
    for key in ("id", "workbook_count", "total_rows", "valid_rows", "invalid_rows", "duplicate_rows", "matched_tasks", "updated_tasks", "skipped_tasks"):
        data[key] = int(data[key] or 0)
    for key in ("created_at", "completed_at"):
        if data[key] is not None:
            data[key] = data[key].isoformat() + "Z"
    # created_by is an internal account id and is not part of the safe summary.
    data.pop("created_by", None)
    return data


def should_apply_self_owned_result(current_result: Any, has_local_change: bool) -> bool:
    """只允许辅助名单填充空白或“无法核实”，且不覆盖平台本地编辑。"""
    if has_local_change:
        return False
    normalized = str(current_result or "").strip()
    return not normalized or normalized == "无法核实"


async def latest_batch(cur) -> dict[str, Any] | None:
    await cur.execute(
        "SELECT id,file_name,file_sha256,rule_version,status,workbook_count,total_rows,valid_rows,"
        "invalid_rows,duplicate_rows,matched_tasks,updated_tasks,skipped_tasks,created_by,created_at,"
        "completed_at,error_message FROM _qmf_self_owned_batches ORDER BY id DESC LIMIT 1"
    )
    return _public_batch(await cur.fetchone())


async def apply_self_owned_import(conn, *, parsed: ParsedSelfOwned, file_name: str, user_id: int | None) -> dict[str, Any]:
    parser = get_parser(MODEL_THREE_PARSER)
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM _qmf_self_owned_batches WHERE file_sha256=%s LIMIT 1",
                (parsed.file_sha256,),
            )
            existing = await cur.fetchone()
            if existing:
                raise SelfOwnedImportError("该名单文件已经导入过")
            await cur.execute(
                "INSERT INTO _qmf_self_owned_batches (file_name,file_sha256,rule_version,status,workbook_count,total_rows,valid_rows,invalid_rows,duplicate_rows,created_by,completed_at) "
                "VALUES (%s,%s,%s,'processing',%s,%s,%s,%s,%s,%s,NULL)",
                (file_name[:255], parsed.file_sha256, RULE_VERSION, parsed.workbook_count,
                 parsed.total_rows, parsed.valid_rows, parsed.invalid_rows, parsed.duplicate_rows, user_id),
            )
            batch_id = int(cur.lastrowid)
            await cur.executemany(
                "INSERT INTO _qmf_self_owned_identities (batch_id,identity_hmac,identity_hmac_version) VALUES (%s,%s,%s)",
                [(batch_id, digest, version) for digest, version in parsed.identities],
            )
            await cur.execute(
                "SELECT projection.row_key, projection.values_json "
                "FROM _online_source_projection AS projection "
                "JOIN _qmf_self_owned_identities AS roster ON roster.identity_hmac=projection.identity_hmac "
                "WHERE projection.parser_type=%s AND roster.batch_id=%s",
                (MODEL_THREE_PARSER, batch_id),
            )
            matched = await cur.fetchall()
            # 资产资料同时进入辖区人员档案和人员标签库。标签表保留原有
            # person_id 语义，通过 registry_person_id 建立可追溯关联。
            await cur.execute(
                "SELECT id FROM watch_categories WHERE code=%s AND is_active=1 FOR UPDATE",
                ("self_owned_resident",),
            )
            category_row = await cur.fetchone()
            if not category_row:
                raise SelfOwnedImportError("自购自住人员标签分类未初始化")
            self_owned_category_id = int(category_row[0])
            identity_names: dict[str, str] = dict(parsed.names)
            await cur.execute(
                "SELECT projection.identity_hmac, projection.values_json "
                "FROM _online_source_projection AS projection "
                "JOIN _qmf_self_owned_identities AS roster ON roster.identity_hmac=projection.identity_hmac "
                "WHERE projection.parser_type=%s AND roster.batch_id=%s",
                (MODEL_THREE_PARSER, batch_id),
            )
            for digest, raw_values in await cur.fetchall():
                values = raw_values if isinstance(raw_values, dict) else json.loads(raw_values or "{}")
                name = str(values.get("姓名") or values.get("name") or "").strip()
                if digest and name:
                    identity_names.setdefault(digest, name[:100])
            registry_people_created = registry_people_reused = 0
            tag_people_created = tag_people_reused = tag_assignments_created = 0
            now = datetime.utcnow()
            for digest, _version in parsed.identities:
                display_name = identity_names.get(digest) or "自购自住人员"
                await cur.execute(
                    "SELECT id, name FROM registry_housing_people WHERE identity_hmac=%s FOR UPDATE",
                    (digest,),
                )
                registry_row = await cur.fetchone()
                if registry_row:
                    registry_person_id = int(registry_row[0])
                    registry_people_reused += 1
                else:
                    await cur.execute(
                        "INSERT INTO registry_housing_people "
                        "(name, identity_number, identity_hmac, identity_hmac_version, verification_status, "
                        "source_type, source_ref, created_by, updated_by) "
                        "VALUES (%s,NULL,%s,%s,'verified','self_owned_asset',%s,%s,%s)",
                        (display_name, digest, _version, f"batch:{batch_id}", user_id, user_id),
                    )
                    registry_person_id = int(cur.lastrowid)
                    registry_people_created += 1
                await cur.execute(
                    "SELECT id, status FROM watch_people WHERE identity_hmac=%s FOR UPDATE",
                    (digest,),
                )
                watch_row = await cur.fetchone()
                if watch_row:
                    watch_person_id = int(watch_row[0])
                    tag_people_reused += 1
                    await cur.execute(
                        "UPDATE watch_people SET registry_person_id=COALESCE(registry_person_id,%s) "
                        "WHERE id=%s",
                        (registry_person_id, watch_person_id),
                    )
                else:
                    await cur.execute(
                        "INSERT INTO watch_people "
                        "(name, identity_number, identity_hmac, identity_hmac_version, registry_person_id, "
                        "verification_status, source_type, source_ref, created_by, updated_by) "
                        "VALUES (%s,NULL,%s,%s,%s,'verified','self_owned_asset',%s,%s,%s)",
                        (display_name, digest, _version, registry_person_id, f"batch:{batch_id}", user_id, user_id),
                    )
                    watch_person_id = int(cur.lastrowid)
                    tag_people_created += 1
                await cur.execute(
                    "SELECT id FROM watch_assignments WHERE person_id=%s AND category_id=%s "
                    "AND status='active' AND valid_from<=UTC_TIMESTAMP() "
                    "AND (valid_to IS NULL OR valid_to>=UTC_TIMESTAMP()) "
                    "AND (released_at IS NULL OR released_at>UTC_TIMESTAMP()) LIMIT 1 FOR UPDATE",
                    (watch_person_id, self_owned_category_id),
                )
                if not await cur.fetchone() and str(watch_row[1] if watch_row else "active") == "active":
                    await cur.execute(
                        "INSERT INTO watch_assignments "
                        "(person_id, category_id, valid_from, source_type, source_ref, basis, created_by, updated_by) "
                        "VALUES (%s,%s,%s,'self_owned_asset',%s,%s,%s,%s)",
                        (watch_person_id, self_owned_category_id, now, f"batch:{batch_id}",
                         "辖区资产资料：自购自住", user_id, user_id),
                    )
                    assignment_id = int(cur.lastrowid)
                    await cur.execute(
                        "INSERT INTO watch_assignment_versions "
                        "(assignment_id, version_no, snapshot_json, changed_by) VALUES (%s,1,%s,%s)",
                        (assignment_id, "{}", user_id),
                    )
                    tag_assignments_created += 1
            updated = skipped = 0
            for row_key, raw_values in matched:
                values = raw_values if isinstance(raw_values, dict) else json.loads(raw_values or "{}")
                current = str(values.get("核查结果") or "").strip()
                if current and current != "无法核实":
                    skipped += 1
                    continue
                await cur.execute(
                    "SELECT 1 FROM _online_local_changes WHERE parser_type=%s AND row_key=%s "
                    "AND field_name='核查结果' AND status IN ('pending','processing','retry','conflict') LIMIT 1",
                    (MODEL_THREE_PARSER, str(row_key)),
                )
                if not should_apply_self_owned_result(current, bool(await cur.fetchone())):
                    skipped += 1
                    continue
                await cur.execute(
                    "SELECT id,values_json FROM _online_source_rows WHERE parser_type=%s AND row_key=%s",
                    (MODEL_THREE_PARSER, str(row_key)),
                )
                source_rows = await cur.fetchall()
                if not source_rows:
                    continue
                for source_id, source_json in source_rows:
                    source_values = source_json if isinstance(source_json, dict) else json.loads(source_json or "{}")
                    source_values["核查结果"] = "近期返吴"
                    encoded = json.dumps(source_values, ensure_ascii=False, sort_keys=True)
                    await cur.execute(
                        "UPDATE _online_source_rows SET values_json=%s,row_hash=%s,revision=revision+1,refreshed_at=UTC_TIMESTAMP() WHERE id=%s",
                        (encoded, source_row_hash(source_values), int(source_id)),
                    )
                await cur.execute(
                    f"UPDATE `{parser.table_name}` SET `核查结果`=%s WHERE `_row_key`=%s",
                    ("近期返吴", str(row_key)),
                )
                updated += 1
            await rebuild_projection(cur, MODEL_THREE_PARSER, reconcile_graph=False)
            await cur.execute(
                "UPDATE _qmf_self_owned_batches SET status='completed',matched_tasks=%s,updated_tasks=%s,skipped_tasks=%s,completed_at=UTC_TIMESTAMP() WHERE id=%s",
                (len(matched), updated, skipped, batch_id),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {
        "batch_id": batch_id,
        "status": "completed",
        "rule_version": RULE_VERSION,
        "workbook_count": parsed.workbook_count,
        "total_rows": parsed.total_rows,
        "valid_rows": parsed.valid_rows,
        "invalid_rows": parsed.invalid_rows,
        "duplicate_rows": parsed.duplicate_rows,
        "matched_tasks": len(matched),
        "updated_tasks": updated,
        "skipped_tasks": skipped,
        "registry_people_created": registry_people_created,
        "registry_people_reused": registry_people_reused,
        "tag_people_created": tag_people_created,
        "tag_people_reused": tag_people_reused,
        "tag_assignments_created": tag_assignments_created,
    }
