"""Deterministic, explicitly fictional shadow data manifest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path


BUSINESS_TYPES = (
    "全链条",
    "出租房屋核查",
    "寄递业",
    "疑似返苏",
    "苏州涉警",
    "交通涉警",
)


@dataclass(frozen=True)
class ShadowUser:
    username: str
    role: str
    community_index: int
    device_id: str


def _password(username: str) -> str:
    # The fixture stores no password.  This deterministic hint is only used by
    # an operator-side seeder to derive a temporary password, never in logs.
    return f"LoadTest-{hashlib.sha256(username.encode()).hexdigest()[:16]}!"


def make_users() -> list[dict[str, str | int]]:
    users: list[dict[str, str | int]] = [
        {"username": "observer@shadow", "role": "super_admin", "community_index": -1, "device_id": "shadow-observer"},
    ]
    groups = (
        ("member", 35),
        ("leader", 8),
        ("internal_business", 4),
        ("admin", 2),
        ("super_admin", 1),
    )
    counter = 1
    for role, count in groups:
        for _ in range(count):
            username = f"loadtest-{role}-{counter:02d}"
            users.append({
                "username": username,
                "role": role,
                "community_index": (counter - 1) % 12,
                "device_id": f"shadow-device-{counter:03d}",
            })
            counter += 1
    for index in range(25):
        username = f"burst-{index + 1:02d}"
        users.append({
            "username": username,
            "role": "member",
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
            tasks.append({
                "parser_type": parser_type,
                "row_key": f"shadow-{parser_type}-{index + 1:04d}",
                "person_name": f"压测人员{task_id:04d}",
                "original_address": f"压测地址{index + 1:04d}号压测小区{index % 48 + 1:02d}幢",
                "community": f"压测社区{index % 12 + 1:02d}",
                "small_community": f"压测小区{index % 48 + 1:02d}",
                "state": state,
                "assigned_user": f"loadtest-member-{(index % 35) + 1:02d}" if state == "assigned" else "",
                "conflict_group": task_id <= 30,
                "version": 1,
            })
            task_id += 1
    return tasks


def write_manifest(output: Path, run_id: str) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "fictional_only": True,
        "users": make_users(),
        "tasks": make_tasks(),
        "communities": [
            {"name": f"压测社区{index:02d}", "properties": [f"压测小区{index * 4 + offset:02d}" for offset in range(1, 5)]}
            for index in range(1, 13)
        ],
    }
    path = output / f"shadow-fixture-{run_id}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return path


def password_hint(username: str) -> str:
    return _password(username)
