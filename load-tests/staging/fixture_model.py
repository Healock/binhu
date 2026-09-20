"""Deterministic, fictional fixtures for the Staging 75-user rehearsal.

The model intentionally contains no passwords and no values copied from a
business database.  Password material is supplied to the private seeder over
stdin and is never written to the runtime index.
"""
from __future__ import annotations

from typing import Any


BUSINESS_TYPES = (
    "全链条",
    "出租房屋核查",
    "寄递业",
    "疑似返苏",
    "苏州涉警",
    "交通涉警",
)


def make_accounts() -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    groups = (
        ("member", 57, "组员", "mine"),
        ("leader", 12, "组长", "community"),
        ("internal_business", 3, "基础管控", "all"),
        ("admin", 2, "所队领导", "all"),
        ("super_admin", 1, "所队领导", "all"),
    )
    ordinal = 0
    for role, count, position, scope in groups:
        for index in range(1, count + 1):
            ordinal += 1
            accounts.append({
                "ordinal": ordinal,
                "username": f"staging-load-{role}-{index:02d}@staging",
                "display_name": f"预发布演练{position}{index:02d}",
                "role": role,
                "position": position,
                "scope": scope,
                "community_index": (ordinal - 1) % 12,
            })
    return accounts


def make_tasks() -> list[dict[str, Any]]:
    members = [item for item in make_accounts() if item["role"] == "member"]
    leaders = [item for item in make_accounts() if item["role"] == "leader"]
    tasks: list[dict[str, Any]] = []
    ordinal = 0
    # 240 rows per business type keeps enough independent write targets for
    # 75 users without turning fixture setup into the load test itself.
    for parser_type in BUSINESS_TYPES:
        for index in range(240):
            ordinal += 1
            community_index = index % 12
            community_members = [item for item in members if item["community_index"] == community_index]
            community_leaders = [item for item in leaders if item["community_index"] == community_index]
            member = community_members[(ordinal - 1) % len(community_members)]
            leader = community_leaders[0]
            scenario = "assigned" if index < 168 else "unassigned" if index < 216 else "assignable"
            assigned = member if scenario == "assigned" else None
            tasks.append({
                "ordinal": ordinal,
                "parser_type": parser_type,
                "scenario": scenario,
                "community": f"预发布演练社区{community_index + 1:02d}",
                "person_name": f"预发布虚构人员{ordinal:05d}",
                "identity_number": f"STG{ordinal:015d}",
                "phone": f"000{ordinal:08d}",
                "original_address": (
                    f"预发布演练小区{community_index + 1:02d}"
                    f"演练楼{index % 20 + 1:02d}幢{index % 30 + 1:02d}室"
                ),
                "assigned_username": assigned["username"] if assigned else "",
                "assigned_user": assigned["display_name"] if assigned else "",
                "claim_username": member["username"] if scenario == "unassigned" else "",
                "assigner_username": leader["username"] if scenario == "assignable" else "",
                "assignment_inspector": leader["display_name"],
                "editable_field": "现住址",
            })
    return tasks


def runtime_index(run_id: str, seeded_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    accounts = make_accounts()
    return {
        "schema_version": 1,
        "run_id": run_id,
        "environment": "staging",
        "fictional_only": True,
        "production_data": False,
        "accounts": [
            {"username": item["username"], "role": item["role"], "scope": item["scope"]}
            for item in accounts
        ],
        "tasks": seeded_tasks,
        "property_search_keyword": "预发布演练小区",
    }
