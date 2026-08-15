"""房屋档案来源导入的纯业务规则。

这里不访问数据库，便于在预览接口和测试中复用同一套分类口径。
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any, Iterable


ISSUE_CERTIFICATE_DUPLICATE = "certificate_duplicate"
ISSUE_CERTIFICATE_CONTENT_CONFLICT = "certificate_content_conflict"
ISSUE_CERTIFICATE_NON_RENTAL = "certificate_non_rental"
ISSUE_HOUSEHOLD_DUPLICATE = "household_duplicate"
ISSUE_HOUSEHOLD_MISSING_TYPE = "household_missing_type"

ISSUE_LABELS = {
    ISSUE_CERTIFICATE_DUPLICATE: "告知书重复记录",
    ISSUE_CERTIFICATE_CONTENT_CONFLICT: "告知书内容不一致",
    ISSUE_CERTIFICATE_NON_RENTAL: "告知书非出租/其他房屋",
    ISSUE_HOUSEHOLD_DUPLICATE: "户号表重复来源",
    ISSUE_HOUSEHOLD_MISSING_TYPE: "户号表未标注类型",
}

NORMAL_HOUSING_TYPES = {"个人出租", "单位出租", "自购房屋", "借住", "其他", "其它"}


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u3000", " ")).strip()


def normalize_community(value: Any) -> str:
    """只做文本清理，正式社区/别名归属由社区目录解析。"""
    return normalize_text(value)


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
    return normalize_text(value)


def _certificate_signature(row: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (key, normalize_text(value))
        for key, value in sorted(row.items())
        if key not in {"source_row", "_source_row"}
    )


def classify_certificate_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Classify source responsibility-notice rows before touching house records.

    A physical source row is retained as an independent record.  Rows sharing
    the same normalized address are held for review, and content differences
    are additionally reported as conflicts; no de-duplication is performed.
    """
    materialized: list[dict[str, Any]] = []
    groups: dict[str, list[int]] = defaultdict(list)
    for index, raw in enumerate(rows, start=1):
        row = {str(key): value for key, value in raw.items()}
        address = normalize_text(row.get("address") or row.get("dz") or row.get("详细地址"))
        row["address"] = address
        row["community"] = normalize_community(row.get("community") or row.get("sssq") or row.get("社区名称"))
        row["source_row"] = row.get("source_row") or row.get("_source_row") or index
        row["source_key"] = normalize_address(address)
        materialized.append(row)
        if row["source_key"]:
            groups[row["source_key"]].append(len(materialized) - 1)

    issues: list[dict[str, Any]] = []
    blocked: set[int] = set()
    duplicate_groups = 0
    conflict_groups = 0
    for key, indexes in groups.items():
        if len(indexes) < 2:
            continue
        duplicate_groups += 1
        signatures = {_certificate_signature(materialized[index]) for index in indexes}
        has_conflict = len(signatures) > 1
        if has_conflict:
            conflict_groups += 1
        for index in indexes:
            blocked.add(index)
            issues.append({
                "issue_type": ISSUE_CERTIFICATE_DUPLICATE,
                "entity_key": key,
                "source_ref": str(materialized[index].get("source_row") or ""),
                "payload": materialized[index],
                "reason": "同一标准化地址存在多条告知书记录，需人工确认",
            })
            if has_conflict:
                issues.append({
                    "issue_type": ISSUE_CERTIFICATE_CONTENT_CONFLICT,
                    "entity_key": key,
                    "source_ref": str(materialized[index].get("source_row") or ""),
                    "payload": materialized[index],
                    "reason": "同一标准化地址的告知书内容不一致，需人工判断",
                })

    normal_rows = [row for index, row in enumerate(materialized) if index not in blocked]
    return {
        "rows": materialized,
        "normal_rows": normal_rows,
        "issues": issues,
        "duplicate_groups": duplicate_groups,
        "conflict_groups": conflict_groups,
        "problem_row_count": len(blocked),
        "normal_count": len(normal_rows),
        "issue_count": len(issues),
    }


def classify_household_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """分类户号表记录。

    重复地址和空住房类型属于问题数据；包括“借住/其他/其它”在内的非空类型
    都是可导入的正常记录，并原样保留住房类型。
    """
    materialized = []
    groups: dict[str, list[int]] = defaultdict(list)
    for index, raw in enumerate(rows, start=1):
        row = {str(key): value for key, value in raw.items()}
        address = normalize_text(row.get("address") or row.get("出租屋地址") or row.get("详细地址"))
        key = normalize_address(address)
        row["address"] = address
        row["community"] = normalize_community(row.get("community") or row.get("社区名称"))
        row["housing_type"] = normalize_housing_type(row.get("housing_type") or row.get("住房类型"))
        row["source_row"] = row.get("source_row") or index
        row["source_key"] = key
        materialized.append(row)
        if key:
            groups[key].append(len(materialized) - 1)

    issues: list[dict[str, Any]] = []
    issue_indexes: set[int] = set()
    duplicate_groups = 0
    for key, indexes in groups.items():
        if len(indexes) < 2:
            continue
        duplicate_groups += 1
        for position, item_index in enumerate(indexes):
            issue_indexes.add(item_index)
            issues.append({
                "issue_type": ISSUE_HOUSEHOLD_DUPLICATE,
                "entity_key": key,
                "source_ref": str(materialized[item_index].get("source_row") or ""),
                "payload": {**materialized[item_index], "duplicate_group_size": len(indexes), "is_representative": position == 0},
                "reason": "同一标准化地址存在多条户号表来源行，需人工确认代表记录",
            })

    for item_index, row in enumerate(materialized):
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
        "issue_count": len(issues),
        "other_type_count": sum(1 for row in normal_rows if row.get("housing_type") not in {"个人出租", "单位出租", "自购房屋"}),
    }


def issue_type_label(issue_type: str) -> str:
    return ISSUE_LABELS.get(issue_type, issue_type)
