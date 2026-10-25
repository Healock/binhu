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
    fallback_configured: bool
    authoritative_path: str
    adapter_ready: bool
    active: bool
    ready: bool
    status: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "domain": self.domain,
            "read_source": self.read_source,
            "fallback_configured": self.fallback_configured,
            "authoritative_path": self.authoritative_path,
            "adapter_ready": self.adapter_ready,
            "active": self.active,
            "ready": self.ready,
            "status": self.status,
            "reason": self.reason,
        }


def get_cutover_state(settings: Any) -> CutoverState:
    """Return a safe, non-sensitive description of the configured cutover.

    ``ready`` describes readiness of the requested new architecture path, not
    health of the legacy MySQL path. The application remains on MySQL until a
    real read adapter is installed and explicitly activated.
    """
    enabled = bool(getattr(settings, "ARCHITECTURE_UPGRADE_ENABLED", False))
    domain = str(getattr(settings, "ARCHITECTURE_UPGRADE_DOMAIN", "") or "").strip()
    read_source = str(
        getattr(settings, "ARCHITECTURE_UPGRADE_READ_SOURCE", "") or ""
    ).strip().lower()
    fallback_configured = bool(
        getattr(settings, "ARCHITECTURE_UPGRADE_FALLBACK_ENABLED", True)
    )
    if domain != SUPPORTED_DOMAIN:
        return CutoverState(
            enabled=enabled,
            domain=domain,
            read_source=read_source,
            fallback_configured=fallback_configured,
            authoritative_path="mysql",
            adapter_ready=False,
            active=False,
            ready=False,
            status="invalid",
            reason="unsupported_domain",
        )
    if read_source not in SUPPORTED_READ_SOURCES:
        return CutoverState(
            enabled=enabled,
            domain=domain,
            read_source=read_source,
            fallback_configured=fallback_configured,
            authoritative_path="mysql",
            adapter_ready=False,
            active=False,
            ready=False,
            status="invalid",
            reason="unsupported_read_source",
        )
    if not enabled:
        return CutoverState(
            enabled=enabled,
            domain=domain,
            read_source=read_source,
            fallback_configured=fallback_configured,
            authoritative_path="mysql",
            adapter_ready=False,
            active=False,
            ready=False,
            status="disabled",
            reason="upgrade_disabled",
        )
    if read_source == "mysql":
        return CutoverState(
            enabled=enabled,
            domain=domain,
            read_source=read_source,
            fallback_configured=fallback_configured,
            authoritative_path="mysql",
            adapter_ready=False,
            active=False,
            ready=False,
            status="mysql",
            reason="authoritative_mysql_path",
        )
    return CutoverState(
        enabled=enabled,
        domain=domain,
        read_source=read_source,
        fallback_configured=fallback_configured,
        authoritative_path="mysql",
        adapter_ready=False,
        active=False,
        ready=False,
        status="not_ready",
        reason="flink_read_adapter_not_installed",
    )


__all__ = ["CutoverState", "SUPPORTED_DOMAIN", "get_cutover_state"]
