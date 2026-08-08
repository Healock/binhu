"""Normalization and exact-match digests for RegistryData."""

from __future__ import annotations

import hashlib
import hmac
import re

from config import settings


def normalize_identity(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def normalize_phone(value: str | None) -> str:
    return re.sub(r"[^0-9+]+", "", str(value or "").strip())


def hmac_digest(value: str | None, *, kind: str) -> tuple[str | None, int]:
    normalized = normalize_identity(value) if kind == "identity" else normalize_phone(value)
    if not normalized:
        return None, 1
    message = f"registry:{kind}:v1:{normalized}".encode("utf-8")
    digest = hmac.new(
        settings.registry_hmac_key.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    return digest, 1
