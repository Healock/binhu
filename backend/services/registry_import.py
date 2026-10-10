"""房屋档案来源导入的纯业务规则。

这里不访问数据库，便于在预览接口和测试中复用同一套分类口径。
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable


ISSUE_CERTIFICATE_DUPLICATE = "certificate_duplicate"
ISSUE_CERTIFICATE_CONTENT_CONFLICT = "certificate_content_conflict"
ISSUE_CERTIFICATE_NON_RENTAL = "certificate_non_rental"
ISSUE_HOUSEHOLD_DUPLICATE = "household_duplicate"
ISSUE_HOUSEHOLD_MISSING_TYPE = "household_missing_type"
ISSUE_HOUSEHOLD_COMMUNITY_UNRESOLVED = "household_community_unresolved"

ISSUE_LABELS = {
    ISSUE_CERTIFICATE_DUPLICATE: "告知书重复记录",
    ISSUE_CERTIFICATE_CONTENT_CONFLICT: "告知书内容不一致",
    ISSUE_CERTIFICATE_NON_RENTAL: "告知书非出租/其他房屋",
    ISSUE_HOUSEHOLD_DUPLICATE: "户号表重复来源",
    ISSUE_HOUSEHOLD_MISSING_TYPE: "户号表未标注类型",
    ISSUE_HOUSEHOLD_COMMUNITY_UNRESOLVED: "户号表社区待核对",
}

NORMAL_HOUSING_TYPES = {"个人出租", "单位出租", "自购房屋", "借住", "其他", "其它"}

CERTIFICATE_FIELD_LABELS = {
    "czrxm": "房东姓名",
    "landlord_name": "房东姓名",
    "czrzjhm": "房东身份证号",
    "landlord_identity_number": "房东身份证号",
    "sjczrxm": "实际承租人",
    "actual_renter_name": "实际承租人",
    "sjczrzjhm": "承租人身份证号",
    "actual_renter_identity_number": "承租人身份证号",
    "isSign": "签署状态",
    "signed_status": "签署状态",
    "signType": "签署类型",
    "sign_type": "签署类型",
    "signTime": "签署时间",
    "sign_time": "签署时间",
    "signurl": "告知书链接",
    "document_ref": "告知书链接",
}

ISSUE_COMPARE_IGNORED_FIELDS = {
    "source_row", "_source_row", "source_key", "source_ref",
    "source_content_hash", "source_sheet",
    "address", "dz", "详细地址", "community", "sssq", "社区名称", "pcsname",
}


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u3000", " ")).strip()


def normalize_community(value: Any) -> str:
    """只做文本清理，正式社区/别名归属由社区目录解析。"""
    return normalize_text(value)


def household_community_candidates(value: Any) -> list[str]:
    """Return increasingly relaxed community labels for household imports.

    The configured formal name or alias always wins.  Administrative source
    suffixes are only removed as a fallback, so an alias such as ``芦荡社区``
    can still map to ``长板社区`` before the shorter ``芦荡`` is considered.
    """
    exact = normalize_community(value)
    if not exact:
        return []
    candidates = [exact]
    for suffix in ("居民委员会", "村民委员会", "居委会"):
        for candidate in list(candidates):
            if candidate.endswith(suffix):
                shortened = candidate[: -len(suffix)].strip()
                if shortened and shortened not in candidates:
                    candidates.append(shortened)
    for suffix in ("社区", "村"):
        for candidate in list(candidates):
            if candidate.endswith(suffix):
                shortened = candidate[: -len(suffix)].strip()
                if shortened and shortened not in candidates:
                    candidates.append(shortened)
    return candidates


def normalize_address(value: Any) -> str:
    """Return the stable key used by both import preview and source matching.

    Source workbooks contain a mix of full-width punctuation and cosmetic
    separators (for example ``2-2号`` versus ``22号``).  The original analysis
    treated those as the same address, so the production importer must use the
    identical rule or it will silently miss duplicate source rows.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[\s\u3000,，。．.、;；:：()（）\[\]【】\-—_]+", "", text)


def normalize_housing_type(value: Any) -> str:
    text = normalize_text(value)
    return {"个人租赁": "个人出租", "单位租赁": "单位出租"}.get(text, text)


def normalize_household_status(value: Any) -> str:
    text = normalize_text(value)
    if text in {"已注销", "注销", "是"}:
        return "已注销"
    if text in {"未注销", "正常", "有效", "在用", "否"}:
        return "未注销"
    # Numeric codes and absent/unrecognized text cannot prove a source status.
    return ""


def issue_problem_details(
    issue_type: str,
    payload: dict[str, Any],
    *,
    entity_key: str = "",
    group_payloads: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Translate an import issue into user-facing field/value evidence.

    The issue page is an external-system correction checklist.  It therefore
    needs the exact field and current source value, not an opaque internal
    issue code or an invitation to edit RegistryData directly.
    """
    address = normalize_text(
        payload.get("address") or payload.get("dz") or payload.get("详细地址")
        or payload.get("normalized_address") or entity_key
    )
    community = normalize_text(
        payload.get("community") or payload.get("community_name")
        or payload.get("社区名称") or payload.get("sssq")
    )
    housing_type = normalize_housing_type(payload.get("housing_type") or payload.get("住房类型"))

    if issue_type == ISSUE_HOUSEHOLD_MISSING_TYPE:
        return [{"field": "住房类型", "value": housing_type or "（空白）"}]
    if issue_type == ISSUE_HOUSEHOLD_COMMUNITY_UNRESOLVED:
        return [{"field": "所属社区", "value": community or "（空白）"}]
    if issue_type == ISSUE_HOUSEHOLD_DUPLICATE:
        return [{"field": "标准详细地址", "value": address or "（空白）"}]
    if issue_type == ISSUE_CERTIFICATE_DUPLICATE:
        return [{"field": "告知书地址", "value": address or "（空白）"}]
    if issue_type == ISSUE_CERTIFICATE_NON_RENTAL:
        return [{"field": "告知书地址", "value": address or "（空白）"}]
    if issue_type != ISSUE_CERTIFICATE_CONTENT_CONFLICT:
        return [{"field": "来源数据", "value": entity_key or "（无法识别）"}]

    comparable = list(group_payloads or [payload])
    keys = sorted({
        str(key)
        for row in comparable
        for key in row
        if str(key) not in ISSUE_COMPARE_IGNORED_FIELDS
    })
    details: list[dict[str, str]] = []
    for key in keys:
        values = {normalize_text(row.get(key)) for row in comparable}
        if len(values) <= 1:
            continue
        details.append({
            "field": CERTIFICATE_FIELD_LABELS.get(key, key),
            "value": normalize_text(payload.get(key)) or "（空白）",
        })
    return details or [{"field": "告知书内容", "value": "同一地址的多条记录内容不一致"}]


def _certificate_signature(row: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    # Compare business content, not upstream IDs or acquisition metadata.
    from services.registry_certificate_source import certificate_content_hash

    comparable = {**row, "address": normalize_address(row.get("address")),
                  "community": normalize_community(row.get("community"))}
    return (("content", certificate_content_hash(comparable)),)


def _certificate_updated_at(row: dict[str, Any]) -> datetime | None:
    # Signature dates and numeric IDs do not identify the newest house state.
    value = next((row.get(key) for key in ("updateTime", "updatedAt", "updated_at", "update_time")
                  if row.get(key)), None)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (TypeError, ValueError):
        return None


def classify_certificate_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Classify source responsibility-notice rows before touching house records.

    Keep physical rows for audit, but identical notices have one current
    representative. Different content without a reliable upstream version
    remains a conflict; page order is not evidence of recency.
    """
    materialized: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, raw in enumerate(rows, start=1):
        row = {str(key): value for key, value in raw.items()}
        address = normalize_text(row.get("address") or row.get("dz") or row.get("详细地址"))
        row["address"] = address
        row["community"] = normalize_community(row.get("community") or row.get("sssq") or row.get("社区名称"))
        row["source_row"] = row.get("source_row") or row.get("_source_row") or index
        row["source_key"] = normalize_address(address)
        materialized.append(row)
        if row["source_key"]:
            groups[(row["community"], row["source_key"])].append(len(materialized) - 1)

    issues: list[dict[str, Any]] = []
    blocked: set[int] = set()
    superseded: set[int] = set()
    duplicate_groups = 0
    conflict_groups = 0
    for (_community, key), indexes in groups.items():
        if len(indexes) < 2:
            continue
        signatures = {_certificate_signature(materialized[index]) for index in indexes}
        has_conflict = len(signatures) > 1
        if not has_conflict:
            superseded.update(indexes[1:])
            continue
        versions = {index: _certificate_updated_at(materialized[index]) for index in indexes}
        if all(value is not None for value in versions.values()):
            latest = max(versions.values())
            current = [index for index in indexes if versions[index] == latest]
            if len({_certificate_signature(materialized[index]) for index in current}) == 1:
                superseded.update(index for index in indexes if index != current[0])
                continue
        duplicate_groups += 1
        if has_conflict:
            conflict_groups += 1
        for index in indexes:
            blocked.add(index)
            issues.append({
                "issue_type": ISSUE_CERTIFICATE_DUPLICATE,
                "entity_key": key,
                "source_ref": str(
                    materialized[index].get("source_ref")
                    or materialized[index].get("source_row")
                    or ""
                ),
                "payload": materialized[index],
                "reason": "同一标准化地址存在多条告知书记录，需人工确认",
            })
            if has_conflict:
                issues.append({
                    "issue_type": ISSUE_CERTIFICATE_CONTENT_CONFLICT,
                    "entity_key": key,
                    "source_ref": str(
                        materialized[index].get("source_ref")
                        or materialized[index].get("source_row")
                        or ""
                    ),
                    "payload": materialized[index],
                    "reason": "同一标准化地址的告知书内容不一致，需人工判断",
                })

    normal_rows = [row for index, row in enumerate(materialized) if index not in blocked | superseded]
    return {
        "rows": materialized,
        "normal_rows": normal_rows,
        "superseded_count": len(superseded),
        "issues": issues,
        "duplicate_groups": duplicate_groups,
        "conflict_groups": conflict_groups,
        "problem_row_count": len(blocked),
        "normal_count": len(normal_rows),
        "issue_count": len(issues),
    }


def classify_household_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """分类户号表记录。

    同社区同户号重复和空住房类型属于问题数据；无户号时才按社区及地址核查重复。
    不同户号允许共享地址；包括“借住/其他/其它”在内的非空类型
    都是可导入的正常记录，并原样保留住房类型。
    """
    materialized = []
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    addresses: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, raw in enumerate(rows, start=1):
        row = {str(key): value for key, value in raw.items()}
        address = normalize_text(row.get("address") or row.get("出租屋地址") or row.get("详细地址"))
        key = normalize_address(address)
        row["address"] = address
        row["community"] = normalize_community(row.get("community") or row.get("社区名称"))
        raw_type = normalize_text(row.get("housing_type") or row.get("住房类型"))
        row["housing_type"] = normalize_housing_type(raw_type)
        if row["housing_type"] != raw_type:
            row["source_housing_type"] = raw_type
        row["source_row"] = row.get("source_row") or index
        row["source_key"] = key
        materialized.append(row)
        if key:
            addresses[(row["community"], key)].append(len(materialized) - 1)
            number = normalize_text(row.get("house_no"))
            groups[(row["community"], "household" if number else "address", number or key)].append(len(materialized) - 1)

    issues: list[dict[str, Any]] = []
    issue_indexes: set[int] = set()
    duplicate_groups = 0
    for (_, identity_kind, _), indexes in groups.items():
        if len(indexes) < 2:
            continue
        duplicate_groups += 1
        for position, item_index in enumerate(indexes):
            issue_indexes.add(item_index)
            issues.append({
                "issue_type": ISSUE_HOUSEHOLD_DUPLICATE,
                "entity_key": materialized[item_index]["source_key"],
                "source_ref": str(materialized[item_index].get("source_row") or ""),
                "payload": {**materialized[item_index], "duplicate_group_size": len(indexes), "is_representative": position == 0},
                "reason": ("同社区同户号存在多条来源行，需核对来源内容"
                           if identity_kind == "household" else "缺少户号且同社区同地址存在多条来源行，需核对户号"),
            })

    for item_index, row in enumerate(materialized):
        if (not normalize_text(row.get("house_no")) and item_index not in issue_indexes
                and len(addresses.get((row["community"], row["source_key"]), ())) > 1):
            issue_indexes.add(item_index)
            issues.append({
                "issue_type": ISSUE_HOUSEHOLD_DUPLICATE, "entity_key": row["source_key"],
                "source_ref": str(row.get("source_row") or ""), "payload": row,
                "reason": "缺少户号且同社区同地址存在其他来源行，需核对户号",
            })
        if not row.get("housing_type"):
            issue_indexes.add(item_index)
            issues.append({
                "issue_type": ISSUE_HOUSEHOLD_MISSING_TYPE,
                "entity_key": row.get("source_key") or str(row.get("source_row") or ""),
                "source_ref": str(row.get("source_row") or ""),
                "payload": row,
                "reason": "住房类型为空，不能自动判断出租/自购归类",
            })

    normal_rows = [row for index, row in enumerate(materialized) if index not in issue_indexes]
    return {
        "rows": materialized,
        "normal_rows": normal_rows,
        "issues": issues,
        "duplicate_groups": duplicate_groups,
        "normal_count": len(normal_rows),
        "issue_count": len(issue_indexes),
        "other_type_count": sum(1 for row in normal_rows if row.get("housing_type") not in {"个人出租", "单位出租", "自购房屋"}),
    }


def classify_household_file_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep file provenance while suppressing identical repeated household IDs."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        # A previous preview may have persisted a suppression flag. Recompute it.
        row.pop("import_skip", None)
        house_no = normalize_text(row.get("house_no"))
        if house_no:
            groups[(normalize_community(row.get("community")), house_no)].append(row)
    conflicts = []
    skipped = set()
    for grouped in groups.values():
        if len(grouped) < 2:
            continue
        fields = ("community", "police_station", "community_code", "house_no", "landlord",
                  "address", "housing_type", "residence_type", "resident_count", "updated_at", "household_status")
        signatures = {tuple((key in row, normalize_household_status(row.get(key)) or normalize_text(row.get(key))) if key == "household_status"
                            else normalize_housing_type(row.get(key)) if key == "housing_type"
                            else normalize_text(row.get(key)) for key in fields) for row in grouped}
        if len(signatures) == 1:
            for row in grouped[1:]:
                row["import_skip"] = "identical_household"
                skipped.add(id(row))
        else:
            conflicts.append(grouped)
    conflict_ids = {id(row) for grouped in conflicts for row in grouped}
    result = classify_household_rows(row for row in rows if id(row) not in skipped | conflict_ids)
    blocked = set()
    for grouped in conflicts:
        result["duplicate_groups"] += 1
        for row in grouped:
            ref = str(row.get("import_source_ref") or row.get("source_row") or "")
            blocked.add(ref)
            result["issues"].append({
                "issue_type": ISSUE_HOUSEHOLD_DUPLICATE,
                "entity_key": normalize_address(row.get("address")),
                "source_ref": ref, "payload": row,
                "reason": "同社区同户号的来源内容或注销状态不一致，需核对原导出文件；不能按上传顺序覆盖",
            })
    result["rows"].extend(row for row in rows if id(row) in skipped | conflict_ids)
    result["normal_rows"] = [row for row in result["normal_rows"] if row.get("import_source_ref") not in blocked]
    result["normal_count"] = len(result["normal_rows"])
    result["issue_count"] = len({str(item["payload"].get("import_source_ref") or item["source_ref"])
                                 for item in result["issues"]})
    result["other_type_count"] = sum(row.get("housing_type") not in {"个人出租", "单位出租", "自购房屋"} for row in result["normal_rows"])
    result["duplicate_row_count"] = len(skipped)
    return result


def issue_type_label(issue_type: str) -> str:
    return ISSUE_LABELS.get(issue_type, issue_type)
