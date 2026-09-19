"""Health gate for the isolated Dev dual-track resident monitor."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re


RUN_RE = re.compile(r"^dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("invalid heartbeat time")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed.astimezone(timezone.utc)


def heartbeat_is_healthy(
    status_path: Path,
    run_id: str,
    *,
    now: str | None = None,
    max_age_seconds: int = 60,
) -> bool:
    """Return true only for a fresh, matching, zero-difference heartbeat."""
    try:
        if not RUN_RE.fullmatch(run_id) or not 15 <= max_age_seconds <= 300:
            return False
        if status_path.is_symlink() or not status_path.is_file():
            return False
        if status_path.stat().st_size > 4096:
            return False
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        if (
            payload.get("environment") != "development"
            or payload.get("run_id") != run_id
            or payload.get("status") != "running"
            or payload.get("unattributed_difference_count") != 0
        ):
            return False
        current = _utc(now) if now is not None else datetime.now(timezone.utc)
        age = (current - _utc(payload.get("updated_at"))).total_seconds()
        return -5 <= age <= max_age_seconds
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def main() -> None:
    run_id = os.environ.get("DEV_RUN_ID", "")
    root = Path(os.environ.get(
        "DUAL_TRACK_EVIDENCE_DIR", "/var/lib/binhu-dev-event-pipeline/evidence"
    ))
    healthy = (
        os.environ.get("APP_ENVIRONMENT") == "development"
        and heartbeat_is_healthy(root / run_id / "monitor-status.json", run_id)
    )
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
