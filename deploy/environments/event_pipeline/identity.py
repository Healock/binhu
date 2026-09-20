"""Shared non-Production identity rules for the event pipeline artifact."""
from __future__ import annotations

import re


DEV_RUN_RE = re.compile(r"^dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
STAGING_RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")


def validate_identity(environment: object, run_id: object) -> tuple[str, str]:
    env = str(environment or "")
    run = str(run_id or "")
    if env == "development" and DEV_RUN_RE.fullmatch(run):
        return env, run
    if env == "staging" and STAGING_RUN_RE.fullmatch(run):
        return env, run
    raise ValueError("non-Production pipeline identity required")


def environment_for_run_id(run_id: object) -> str:
    run = str(run_id or "")
    if DEV_RUN_RE.fullmatch(run):
        return "development"
    if STAGING_RUN_RE.fullmatch(run):
        return "staging"
    raise ValueError("invalid pipeline run ID")


def topic_for(environment: str) -> str:
    if environment == "development":
        return "dev.task.events.v1"
    if environment == "staging":
        return "staging.task.events.v1"
    raise ValueError("non-Production pipeline topic required")


def source_name(environment: str, engine: str) -> str:
    if environment not in {"development", "staging"} or engine not in {"python", "flink"}:
        raise ValueError("invalid pipeline source identity")
    suffix = "dev" if environment == "development" else "staging"
    return f"{engine}-{suffix}"


__all__ = ["environment_for_run_id", "source_name", "topic_for", "validate_identity"]
