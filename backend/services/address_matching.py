"""本地规则地址匹配层。

首版只负责生成小区建议，不自动改变任务社区或执行分配。实现刻意不假设
地址一定包含街道、道路、门牌、楼栋和房号；这些字段都是可选特征。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Any, Iterable, Protocol


MATCHER_VERSION = "rule-v1"
LOW_INFORMATION_MARKERS = ("派出所", "公司", "厂房", "商场旁", "附近", "日期")
CHINESE_DIGITS = str.maketrans("零〇一二三四五六七八九", "00123456789")


def normalize_address_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = text.translate(CHINESE_DIGITS)
    text = re.sub(r"[\s\u3000]+", "", text)
    text = text.replace("號", "号").replace("栋", "幢").replace("号楼", "幢")
    text = re.sub(r"[，。、“”‘’（）()【】\[\]：:；;、/\\|_-]+", "", text)
    text = text.replace("座", "幢")
    return text


def _extract(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def parse_address(value: Any) -> dict[str, str]:
    original = str(value or "").strip()
    normalized = normalize_address_text(original)
    return {
        "original": original,
        "normalized": normalized,
        "street": _extract(normalized, (r"^(.{1,8}?街道)", r"^(.{1,8}?镇)")),
        "road": _extract(normalized, (r"(.{1,20}?(?:路|街|道))",)),
        "house_number": _extract(normalized, (r"(\d{1,6}号)",)),
        "building": _extract(normalized, (r"(\d{1,4}幢)", r"(\d{1,4}座)")),
        "room": _extract(normalized, (r"(\d{1,4}室)", r"(\d{1,4}号)")),
    }


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    return {text[index:index + 2] for index in range(max(0, len(text) - 1))}


@dataclass(frozen=True)
class AddressCandidate:
    entry_id: int
    name: str
    community_id: int | None
    community_name: str
    score: float
    method: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _entry_parts(entry: dict[str, Any]) -> dict[str, str]:
    aliases = entry.get("aliases") or entry.get("aliases_json") or []
    if isinstance(aliases, str):
        try:
            import json
            aliases = json.loads(aliases)
        except Exception:
            aliases = []
    if isinstance(aliases, dict):
        aliases = list(aliases.values())
    if not isinstance(aliases, list):
        aliases = []
    names = [entry.get("name", ""), entry.get("detail_address", ""), *aliases]
    normalized = [normalize_address_text(name) for name in names if name]
    parsed_detail = parse_address(entry.get("detail_address", ""))
    return {
        "name": normalize_address_text(entry.get("name", "")),
        "detail": normalize_address_text(entry.get("detail_address", "")),
        "aliases": "|".join(normalized[2:]),
        "all": "|".join(normalized),
        "street": normalize_address_text(entry.get("street", "")) or parsed_detail["street"],
        "road": normalize_address_text(entry.get("road", "")) or parsed_detail["road"],
        "house_number": parsed_detail["house_number"],
        "building": parsed_detail["building"],
        "room": parsed_detail["room"],
    }


def _is_low_information(parsed: dict[str, str]) -> bool:
    text = parsed["normalized"]
    if not text or len(text) < 4:
        return True
    return any(marker in text for marker in LOW_INFORMATION_MARKERS) and not (
        parsed.get("road") or parsed.get("house_number")
    )


def match_address(
    address: Any,
    entries: Iterable[dict[str, Any]],
    *,
    community_name: str = "",
    street_name: str = "",
    top_n: int = 50,
) -> dict[str, Any]:
    parsed = parse_address(address)
    if _is_low_information(parsed):
        return {
            "status": "invalid" if parsed["normalized"] else "unmatched",
            "score": 0.0,
            "method": "rule",
            "reason": "地址信息不足或疑似非住宅描述",
            "candidate": None,
            "candidates": [],
            "version": MATCHER_VERSION,
        }
    requested_community = normalize_address_text(community_name)
    requested_street = normalize_address_text(street_name or parsed.get("street"))
    candidates: list[AddressCandidate] = []
    street_conflicts = 0
    community_conflicts = 0
    for raw in entries:
        if not raw.get("enabled", True) or not raw.get("community_id"):
            continue
        parts = _entry_parts(raw)
        candidate_street = parts.get("street", "")
        if requested_street and candidate_street and requested_street != candidate_street:
            street_conflicts += 1
            continue
        candidate_community = normalize_address_text(raw.get("community_name", ""))
        if requested_community and candidate_community and requested_community != candidate_community:
            community_conflicts += 1
            continue
        score = 0.0
        methods: list[str] = []
        if parsed["normalized"] == parts["detail"] and parts["detail"]:
            score += 0.58
            methods.append("完整地址")
        if parts["name"] and parts["name"] in parsed["normalized"]:
            score += 0.48
            methods.append("小区名")
        elif parts["aliases"] and any(alias and alias in parsed["normalized"] for alias in parts["aliases"].split("|")):
            score += 0.42
            methods.append("小区别名")
        if parts["detail"] and parts["detail"] in parsed["normalized"]:
            score += 0.25
            methods.append("地址片段")
        if parsed.get("road") and parts.get("road") and parsed["road"] == parts["road"]:
            score += 0.16
            methods.append("道路")
        if parsed.get("house_number") and parsed["house_number"] in parts["detail"]:
            score += 0.12
            methods.append("门牌")
        if parsed.get("building") and parts.get("building") and parsed["building"] == parts["building"]:
            score += 0.12
            methods.append("楼栋")
        if parsed.get("room") and parts.get("room") and parsed["room"] == parts["room"]:
            score += 0.1
            methods.append("房号")
        overlap = len(_tokens(parsed["normalized"]) & _tokens(parts["all"]))
        union = len(_tokens(parsed["normalized"]) | _tokens(parts["all"])) or 1
        score += min(0.12, 0.12 * overlap / union)
        similarity = SequenceMatcher(None, parsed["normalized"], parts["all"]).ratio()
        score += min(0.08, similarity * 0.08)
        if score <= 0.08:
            continue
        candidates.append(AddressCandidate(
            entry_id=int(raw["id"]),
            name=str(raw.get("name") or ""),
            community_id=int(raw["community_id"]) if raw.get("community_id") is not None else None,
            community_name=str(raw.get("community_name") or ""),
            score=round(min(score, 1.0), 4),
            method="+".join(methods) or "规则",
            reason="；".join(methods) or "字符相似",
        ))
    candidates.sort(key=lambda item: (-item.score, item.name, item.entry_id))
    candidates = candidates[:top_n]
    if not candidates:
        if street_conflicts:
            return {
                "status": "conflict", "score": 0.0, "method": "rule",
                "reason": "任务地址与候选小区街道不一致", "candidate": None,
                "candidates": [], "version": MATCHER_VERSION,
            }
        if community_conflicts:
            return {
                "status": "conflict", "score": 0.0, "method": "rule",
                "reason": "任务社区与候选小区归属不一致", "candidate": None,
                "candidates": [], "version": MATCHER_VERSION,
            }
        return {
            "status": "unmatched", "score": 0.0, "method": "rule",
            "reason": "没有可靠的小区候选", "candidate": None,
            "candidates": [], "version": MATCHER_VERSION,
        }
    best = candidates[0]
    same_name_communities = {item.community_id for item in candidates if item.name == best.name}
    if len(same_name_communities) > 1:
        status = "conflict"
        reason = "同名小区对应多个社区"
    elif len(candidates) > 1 and best.score - candidates[1].score < 0.08:
        status = "ambiguous"
        reason = "前两名候选分数接近，需要人工确认"
    elif best.score >= 0.55 and ("小区名" in best.method or "完整地址" in best.method):
        status = "suggested"
        reason = "关键地址要素一致，生成系统建议"
    else:
        status = "ambiguous"
        reason = "信息不足，生成候选但不自动确认"
    return {
        "status": status,
        "score": best.score,
        "method": best.method,
        "reason": reason,
        "candidate": best.as_dict(),
        "candidates": [item.as_dict() for item in candidates],
        "version": MATCHER_VERSION,
    }


class AddressMatcher(Protocol):
    version: str

    def match(self, address: Any, entries: Iterable[dict[str, Any]], **kwargs) -> dict[str, Any]: ...


class RuleMatcher:
    version = MATCHER_VERSION

    def match(self, address: Any, entries: Iterable[dict[str, Any]], **kwargs) -> dict[str, Any]:
        return match_address(address, entries, **kwargs)


class ElasticsearchMatcher:
    """后续召回适配器占位；首版不建立运行依赖。"""

    version = "disabled"

    def match(self, address: Any, entries: Iterable[dict[str, Any]], **kwargs) -> dict[str, Any]:
        raise RuntimeError("Elasticsearch 地址召回尚未启用")


class MGeoReranker:
    """后续语义精排适配器占位；规则硬约束仍由调用方执行。"""

    version = "disabled"

    def rerank(self, address: Any, candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        raise RuntimeError("MGeo 地址精排尚未启用")
