"""Fixed, auditable workload profiles for Staging load acceptance."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class LoadProfile:
    name: str
    users: int
    spawn_rate: int
    duration_seconds: int
    role_counts: tuple[tuple[str, int], ...]
    equivalent_to_full_gate: bool


PROFILES = {
    "full75": LoadProfile(
        name="full75", users=75, spawn_rate=5, duration_seconds=1800,
        role_counts=(("member", 57), ("leader", 12), ("internal_business", 3),
                     ("admin", 2), ("super_admin", 1)),
        equivalent_to_full_gate=True,
    ),
    "low20": LoadProfile(
        name="low20", users=20, spawn_rate=2, duration_seconds=600,
        role_counts=(("member", 12), ("leader", 4), ("internal_business", 2),
                     ("admin", 1), ("super_admin", 1)),
        equivalent_to_full_gate=False,
    ),
}


def get_profile(name: str) -> LoadProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError("unsupported_staging_load_profile") from None


def select_accounts(accounts: list[dict], profile: LoadProfile) -> list[dict]:
    selected: list[dict] = []
    for role, required in profile.role_counts:
        matching = [item for item in accounts if item.get("role") == role]
        if len(matching) < required:
            raise ValueError("staging_profile_role_coverage_insufficient")
        selected.extend(matching[:required])
    if len(selected) != profile.users:
        raise ValueError("staging_profile_account_count_mismatch")
    return selected


def profile_payload(profile: LoadProfile) -> dict[str, object]:
    return {
        "name": profile.name,
        "users": profile.users,
        "spawn_rate": profile.spawn_rate,
        "duration_seconds": profile.duration_seconds,
        "equivalent_to_full_75_user_gate": profile.equivalent_to_full_gate,
        "scope_note": ("Full 75-user gate." if profile.equivalent_to_full_gate else
                       "Low-load functional and resource acceptance only; not equivalent to the 75-user gate."),
    }


if __name__ == "__main__":
    print(json.dumps(profile_payload(get_profile(sys.argv[1] if len(sys.argv) == 2 else ""))))
