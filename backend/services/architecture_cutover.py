"""Fail-closed state for the first Kafka/Flink/Redis cutover domain.

The event pipeline has a usable task-metadata projection path in the isolated
Development environment. Staging acceptance and broad business adoption are
still pending. This module exposes that scoped availability boundary without
claiming that every business domain has migrated.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED_DOMAIN = "task_metadata"
SUPPORTED_DOMAINS = frozenset({"task_metadata", "online_source"})
SUPPORTED_READ_SOURCES = frozenset({"mysql", "flink"})


@dataclass(frozen=True)
class CutoverState:
    enabled: bool
    domain: str
    read_source: str
    fallback_configured: bool
    authoritative_path: str
    available: bool
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
            "available": self.available,
            "adapter_ready": self.adapter_ready,
            "active": self.active,
            "ready": self.ready,
            "status": self.status,
            "reason": self.reason,
        }


def get_cutover_state(settings: Any) -> CutoverState:
    """Return a safe, non-sensitive description of the configured cutover.

    ``available`` describes the validated scoped architecture capability;
    ``active`` describes whether application reads have been moved to it.
    These are separate so limited adoption is not reported as unavailable.
    """
    enabled = bool(getattr(settings, "ARCHITECTURE_UPGRADE_ENABLED", False))
    domain = str(getattr(settings, "ARCHITECTURE_UPGRADE_DOMAIN", "") or "").strip()
    read_source = str(
        getattr(settings, "ARCHITECTURE_UPGRADE_READ_SOURCE", "") or ""
    ).strip().lower()
    fallback_configured = bool(
        getattr(settings, "ARCHITECTURE_UPGRADE_FALLBACK_ENABLED", True)
    )
    if domain not in SUPPORTED_DOMAINS:
        return CutoverState(
            enabled=enabled,
            domain=domain,
            read_source=read_source,
            fallback_configured=fallback_configured,
            authoritative_path="mysql",
            available=False,
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
            available=False,
            adapter_ready=False,
            active=False,
            ready=False,
            status="invalid",
            reason="unsupported_read_source",
        )
    runtime_contract_ready = bool(
        getattr(settings, "ARCHITECTURE_UPGRADE_RUN_ID", "")
        and getattr(settings, "ARCHITECTURE_UPGRADE_DERIVED_DATABASE", "")
    )
    active = bool(
        enabled and read_source == "flink"
        and domain == "online_source" and runtime_contract_ready
    )
    return CutoverState(
        enabled=enabled,
        domain=domain,
        read_source=read_source,
        fallback_configured=fallback_configured,
        authoritative_path="mysql",
        available=True,
        adapter_ready=True,
        active=active,
        ready=True,
        status="active" if active else "available",
        reason=(
            "active_flink_projection"
            if active else
            "flink_runtime_contract_missing"
            if enabled and read_source == "flink" and domain == "online_source" else
            "validated_scoped_domain_not_active"
            if not enabled or read_source == "mysql"
            else "validated_scoped_domain_pending_read_activation"
        ),
    )


__all__ = ["CutoverState", "SUPPORTED_DOMAIN", "SUPPORTED_DOMAINS", "get_cutover_state"]
