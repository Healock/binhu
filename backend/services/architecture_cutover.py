"""Fail-closed state for the first Kafka/Flink/Redis cutover domain.

The event pipeline currently proves metadata projection only in isolated
Development/Staging environments.  This module exposes that boundary to the
application without pretending that a Flink read adapter already exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED_DOMAIN = "task_metadata"
SUPPORTED_READ_SOURCES = frozenset({"mysql", "flink"})


@dataclass(frozen=True)
class CutoverState:
    enabled: bool
    domain: str
    read_source: str
    fallback_enabled: bool
    ready: bool
    status: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "domain": self.domain,
            "read_source": self.read_source,
            "fallback_enabled": self.fallback_enabled,
            "ready": self.ready,
            "status": self.status,
            "reason": self.reason,
        }


def get_cutover_state(settings: Any) -> CutoverState:
    """Return a safe, non-sensitive description of the configured cutover.

    A Flink read source is intentionally reported as unavailable until an
    application adapter is added.  Callers must keep using MySQL when
    ``ready`` is false; no environment can turn this into an implicit switch.
    """
    enabled = bool(getattr(settings, "ARCHITECTURE_UPGRADE_ENABLED", False))
    domain = str(getattr(settings, "ARCHITECTURE_UPGRADE_DOMAIN", "") or "").strip()
    read_source = str(
        getattr(settings, "ARCHITECTURE_UPGRADE_READ_SOURCE", "") or ""
    ).strip().lower()
    fallback_enabled = bool(
        getattr(settings, "ARCHITECTURE_UPGRADE_FALLBACK_ENABLED", True)
    )
    if domain != SUPPORTED_DOMAIN:
        return CutoverState(
            enabled=enabled,
            domain=domain,
            read_source=read_source,
            fallback_enabled=fallback_enabled,
            ready=False,
            status="invalid",
            reason="unsupported_domain",
        )
    if read_source not in SUPPORTED_READ_SOURCES:
        return CutoverState(
            enabled=enabled,
            domain=domain,
            read_source=read_source,
            fallback_enabled=fallback_enabled,
            ready=False,
            status="invalid",
            reason="unsupported_read_source",
        )
    if not enabled or read_source == "mysql":
        return CutoverState(
            enabled=enabled,
            domain=domain,
            read_source=read_source,
            fallback_enabled=fallback_enabled,
            ready=True,
            status="mysql",
            reason="authoritative_mysql_path",
        )
    return CutoverState(
        enabled=enabled,
        domain=domain,
        read_source=read_source,
        fallback_enabled=fallback_enabled,
        ready=False,
        status="not_ready",
        reason="flink_read_adapter_not_installed",
    )


__all__ = ["CutoverState", "SUPPORTED_DOMAIN", "get_cutover_state"]
