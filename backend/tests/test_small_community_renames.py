import os

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from migrations.small_community_renames import _apply_plan, build_parser
from services.small_community_renames import (
    RENAME_RULES,
    build_rename_plans,
    merge_aliases,
    rewrite_candidate_payload,
)


def _entry(entry_id, name, *, community_id=2, aliases=None, address="", enabled=True):
    return {
        "id": entry_id,
        "name": name,
        "community_id": community_id,
        "community_name": "示例社区",
        "aliases": aliases or [],
        "detail_address": address,
        "enabled": enabled,
    }


def test_confirmed_rename_rules_resolve_without_cross_community_guessing():
    plans, issues = build_rename_plans([
        _entry(47, "天健弘悦府", address="龙河路288号"),
        _entry(90, "澄泓悦园（58-63幢）"),
        _entry(39, "华邦中央花园（华邦商务广场）", address="中山南路988号"),
    ])

    assert issues == []
    assert [plan.rule.key for plan in plans] == [rule.key for rule in RENAME_RULES]
    chenghong = plans[0]
    assert chenghong.target["id"] == 90
    assert [source["id"] for source in chenghong.sources] == [47]
    assert chenghong.detail_address == "龙河路288号"
    assert set(chenghong.aliases) == {"天健弘悦府", "澄泓悦园（58-63幢）"}
    huabang = plans[1]
    assert huabang.target["id"] == 39
    assert set(huabang.aliases) == {"华邦商务广场", "华邦中央花园（华邦商务广场）"}


def test_rename_plan_refuses_cross_community_source_target_pair():
    _, issues = build_rename_plans([
        _entry(47, "天健弘悦府", community_id=3),
        _entry(90, "澄泓悦园（58-63幢）", community_id=2),
        _entry(39, "华邦中央花园（华邦商务广场）"),
    ])
    assert {item["rule"] for item in issues} == {"chenghong-garden"}


def test_rename_plan_is_idempotent_after_names_aliases_and_status_are_applied():
    plans, issues = build_rename_plans([
        _entry(47, "天健弘悦府", address="龙河路288号", enabled=False),
        _entry(90, "澄泓悦园", aliases=["天健弘悦府", "澄泓悦园（58-63幢）"], address="龙河路288号"),
        _entry(39, "华邦中央花园", aliases=["华邦商务广场", "华邦中央花园（华邦商务广场）"]),
    ])
    assert issues == []
    assert plans[0].target["id"] == 90
    assert plans[0].sources[0]["enabled"] is False
    assert set(plans[0].aliases) == {"天健弘悦府", "澄泓悦园（58-63幢）"}


def test_rename_plan_refuses_target_without_formal_community():
    _, issues = build_rename_plans([
        _entry(47, "天健弘悦府", community_id=None),
        _entry(90, "澄泓悦园（58-63幢）", community_id=None),
        _entry(39, "华邦中央花园（华邦商务广场）"),
    ])
    assert {item["rule"] for item in issues} == {"chenghong-garden"}


def test_alias_merge_is_normalized_deduplicated_and_excludes_canonical_name():
    assert merge_aliases(
        ["旧名", " 旧名 "],
        ["新名", "历史名"],
        canonical_name="新名",
    ) == ("旧名", "历史名")


def test_candidate_evidence_rewrites_old_ids_and_collapses_duplicate_target():
    payload = {
        "candidates": [
            {"entry_id": 47, "name": "天健弘悦府", "community_id": 2, "score": 0.8},
            {"entry_id": 90, "name": "澄泓悦园（58-63幢）", "community_id": 2, "score": 0.7},
        ]
    }
    rewritten = rewrite_candidate_payload(
        payload,
        affected_ids=(47, 90),
        target_id=90,
        canonical_name="澄泓悦园",
        community_id=2,
        community_name="示例社区",
    )
    assert rewritten["candidates"] == [{
        "entry_id": 90,
        "name": "澄泓悦园",
        "community_id": 2,
        "community_name": "示例社区",
        "score": 0.8,
    }]


def test_migration_requires_explicit_apply_flag():
    parser = build_parser()
    assert parser.parse_args(["migrate"]).apply is False
    assert parser.parse_args(["migrate", "--apply"]).apply is True


@pytest.mark.asyncio
async def test_identity_merge_never_updates_manual_confirmation_metadata():
    plans, issues = build_rename_plans([
        _entry(47, "天健弘悦府", address="龙河路288号"),
        _entry(90, "澄泓悦园（58-63幢）"),
        _entry(39, "华邦中央花园（华邦商务广场）"),
    ])
    assert issues == []

    class Cursor:
        def __init__(self):
            self.calls = []

        async def execute(self, sql, params=None):
            self.calls.append((" ".join(str(sql).split()), params))

        async def executemany(self, sql, params=None):
            self.calls.append((" ".join(str(sql).split()), params))

        async def fetchall(self):
            return []

    cursor = Cursor()
    await _apply_plan(cursor, plans[0])
    update_sql = "\n".join(sql for sql, _ in cursor.calls if sql.startswith("UPDATE"))
    assert "confirmed_by=" not in update_sql
    assert "confirmed_at=" not in update_sql
    assert "match_status=" not in update_sql
    assert "small_community_id=%s,small_community_name=%s" in update_sql
