"""Deterministic, explicitly fictional shadow data manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


BUSINESS_TYPES = (
    "全链条",
    "出租房屋核查",
    "寄递业",
    "疑似返苏",
    "苏州涉警",
    "交通涉警",
)

CORE_MEMBER_COUNT = 35
BURST_MEMBER_COUNT = 25


def _worker(index: int) -> tuple[str, str, int]:
    """Return username, display name and zero-based community for a writer."""
    worker_index = index % (CORE_MEMBER_COUNT + BURST_MEMBER_COUNT)
    if worker_index < CORE_MEMBER_COUNT:
        number = worker_index + 1
        return (
            f"loadtest-member-{number:02d}",
            f"压测组员{number:02d}",
            worker_index % 12,
        )
    number = worker_index - CORE_MEMBER_COUNT + 1
    return (
        f"burst-{number:02d}",
        f"压测突发组员{number:02d}",
        worker_index % 12,
    )


def _password(username: str) -> str:
    # The fixture stores no password.  This deterministic hint is only used by
    # an operator-side seeder to derive a temporary password, never in logs.
    return f"LoadTest-{hashlib.sha256(username.encode()).hexdigest()[:16]}!"


def make_users() -> list[dict[str, str | int]]:
    users: list[dict[str, str | int]] = [
        {
            "username": "observer@shadow", "display_name": "影子环境观察员",
            "role": "super_admin", "position": "所队领导", "community_index": -1,
            "device_id": "shadow-observer",
        },
    ]
    groups = (
        ("member", 35),
        ("leader", 8),
        ("internal_business", 4),
        ("admin", 2),
        ("super_admin", 1),
    )
    for role, count in groups:
        for index in range(1, count + 1):
            username = f"loadtest-{role}-{index:02d}"
            position = {
                "member": "组员", "leader": "组长", "internal_business": "基础管控",
                "admin": "所队领导", "super_admin": "所队领导",
            }[role]
            display_label = {
                "member": "组员", "leader": "组长", "internal_business": "基础管控",
                "admin": "管理员", "super_admin": "超级管理员",
            }[role]
            users.append({
                "username": username,
                "display_name": f"压测{display_label}{index:02d}",
                "role": role,
                "position": position,
                "community_index": (index - 1) % 12,
                "device_id": f"shadow-{role}-{index:03d}",
            })
    for index in range(25):
        username = f"burst-{index + 1:02d}"
        users.append({
            "username": username,
            "display_name": f"压测突发组员{index + 1:02d}",
            "role": "member",
            "position": "组员",
            "community_index": index % 12,
            "device_id": f"shadow-burst-{index + 1:03d}",
        })
    return users


def make_tasks() -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    task_id = 1
    for parser_type in BUSINESS_TYPES:
        for index in range(600):
            if index < 450:
                state = "assigned"
            elif index < 510:
                state = "unassigned"
            elif index < 540:
                state = "pending_registration"
            elif index < 570:
                state = "unverifiable"
            else:
                state = "completed"
            assigned_username = assigned_display_name = ""
            if state != "unassigned":
                assigned_username, assigned_display_name, worker_community = _worker(
                    task_id - 1
                )
                community_index = worker_community + 1
            else:
                community_index = index % 12 + 1
            property_index = (
                (community_index - 1) * 4 + ((index // 12) % 4) + 1
            )
            tasks.append({
                "ordinal": task_id,
                "parser_type": parser_type,
                "person_name": f"压测人员{task_id:04d}",
                "identity_number": f"LT{task_id:016d}",
                "phone": f"199{task_id:08d}",
                "original_address": f"压测小区{property_index:02d}压测楼{index % 20 + 1:02d}幢{index % 30 + 1:02d}室",
                "community": f"压测社区{community_index:02d}",
                "small_community": f"压测小区{property_index:02d}",
                "property_index": property_index,
                "state": state,
                "assigned_username": assigned_username,
                "assigned_user": assigned_display_name,
                # Exactly thirty shared conflict targets across the whole fixture.
                "conflict_group": task_id <= 30,
                "version": 1,
            })
            task_id += 1
    return tasks


def write_manifest(output: Path, run_id: str) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "fictional_only": True,
        "users": make_users(),
        "tasks": make_tasks(),
        "communities": [
            {"name": f"压测社区{index:02d}", "properties": [f"压测小区{(index - 1) * 4 + offset:02d}" for offset in range(1, 5)]}
            for index in range(1, 13)
        ],
    }
    path = output / f"shadow-fixture-{run_id}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return path


def password_hint(username: str) -> str:
    return _password(username)
