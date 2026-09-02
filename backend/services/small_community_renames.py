"""Canonical small-community rename rules and migration helpers.

The address library keeps historical names as aliases.  A rename may also
merge an older address-library row into the canonical row, but it must never
erase manual confirmation metadata attached to task or property matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from services.police_dispatch import normalize_lookup


@dataclass(frozen=True)
class SmallCommunityRenameRule:
    key: str
    canonical_name: str
    target_names: tuple[str, ...]
    historical_aliases: tuple[str, ...]
    source_names: tuple[str, ...] = ()
    inherit_source_address: bool = False


@dataclass(frozen=True)
class SmallCommunityRenamePlan:
    rule: SmallCommunityRenameRule
    target: dict[str, Any]
    sources: tuple[dict[str, Any], ...]
    aliases: tuple[str, ...]
    detail_address: str


RENAME_RULES = (
    SmallCommunityRenameRule(
        key="chenghong-garden",
        canonical_name="澄泓悦园",
        target_names=("澄泓悦园", "澄泓悦园（58-63幢）"),
        historical_aliases=("天健弘悦府", "澄泓悦园（58-63幢）"),
        source_names=("天健弘悦府",),
        inherit_source_address=True,
    ),
    SmallCommunityRenameRule(
        key="huabang-central-garden",
        canonical_name="华邦中央花园",
        target_names=("华邦中央花园", "华邦中央花园（华邦商务广场）"),
        historical_aliases=("华邦商务广场", "华邦中央花园（华邦商务广场）"),
    ),
)


def _normalized_set(values: Iterable[str]) -> set[str]:
    return {normalize_lookup(value) for value in values if normalize_lookup(value)}


def merge_aliases(*groups: Iterable[str], canonical_name: str) -> tuple[str, ...]:
    canonical = normalize_lookup(canonical_name)
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            cleaned = str(value or "").strip()
            normalized = normalize_lookup(cleaned)
            if not cleaned or not normalized or normalized == canonical or normalized in seen:
                continue
            seen.add(normalized)
            result.append(cleaned)
    return tuple(result)


def build_rename_plans(
    entries: Iterable[dict[str, Any]],
    rules: Iterable[SmallCommunityRenameRule] = RENAME_RULES,
) -> tuple[list[SmallCommunityRenamePlan], list[dict[str, str]]]:
    """Resolve configured renames without guessing across communities."""
    rows = [dict(item) for item in entries]
    plans: list[SmallCommunityRenamePlan] = []
    issues: list[dict[str, str]] = []
    for rule in rules:
        target_names = _normalized_set(rule.target_names)
        source_names = _normalized_set(rule.source_names)
        targets = [row for row in rows if normalize_lookup(row.get("name")) in target_names]
        sources = [row for row in rows if normalize_lookup(row.get("name")) in source_names]

        if rule.source_names and sources:
            source_communities = {row.get("community_id") for row in sources}
            targets = [row for row in targets if row.get("community_id") in source_communities]
        if len(targets) != 1:
            issues.append({
                "rule": rule.key,
                "reason": "找不到唯一的现小区记录" if not targets else "现小区记录不唯一",
            })
            continue
        target = targets[0]
        if target.get("community_id") is None:
            issues.append({"rule": rule.key, "reason": "现小区缺少正式社区归属"})
            continue
        same_community_sources = tuple(
            row for row in sources
            if row.get("community_id") == target.get("community_id")
            and int(row.get("id") or 0) != int(target.get("id") or 0)
        )
        if rule.source_names and sources and len(same_community_sources) != len(sources):
            issues.append({"rule": rule.key, "reason": "旧名与现名所属社区不一致"})
            continue

        existing_aliases = target.get("aliases") or []
        aliases = merge_aliases(
            existing_aliases,
            (str(target.get("name") or ""),),
            rule.historical_aliases,
            (str(row.get("name") or "") for row in same_community_sources),
            canonical_name=rule.canonical_name,
        )
        detail_address = str(target.get("detail_address") or "").strip()
        if not detail_address and rule.inherit_source_address:
            detail_address = next((
                str(row.get("detail_address") or "").strip()
                for row in same_community_sources
                if str(row.get("detail_address") or "").strip()
            ), "")
        plans.append(SmallCommunityRenamePlan(
            rule=rule,
            target=target,
            sources=same_community_sources,
            aliases=aliases,
            detail_address=detail_address,
        ))
    return plans, issues


def rewrite_candidate_payload(
    payload: Any,
    *,
    affected_ids: Iterable[int],
    target_id: int,
    canonical_name: str,
    community_id: int,
    community_name: str,
) -> Any:
    """Rewrite candidate evidence while retaining scores and explanations."""
    affected = {int(value) for value in affected_ids}
    if isinstance(payload, dict):
        return {
            key: rewrite_candidate_payload(
                value,
                affected_ids=affected,
                target_id=target_id,
                canonical_name=canonical_name,
                community_id=community_id,
                community_name=community_name,
            )
            for key, value in payload.items()
        }
    if not isinstance(payload, list):
        return payload

    rewritten: list[Any] = []
    seen_candidates: set[tuple[int, int]] = set()
    for value in payload:
        item = value
        if isinstance(value, dict):
            item = dict(value)
            entry_id = int(item.get("entry_id") or 0)
            if entry_id in affected:
                item.update({
                    "entry_id": target_id,
                    "name": canonical_name,
                    "community_id": community_id,
                    "community_name": community_name,
                })
            if item.get("entry_id") is not None:
                key = (int(item.get("entry_id") or 0), int(item.get("community_id") or 0))
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
        rewritten.append(item)
    return rewritten
