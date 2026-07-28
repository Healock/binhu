"""Personal-data masking helpers shared by APIs and import results."""

from typing import Any


def mask_identity_number(value: Any) -> str:
    """Return a stable masked value without exposing the full identity number."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) >= 10:
        return f"{text[:6]}{'*' * max(4, len(text) - 10)}{text[-4:]}"
    if len(text) >= 5:
        return f"{text[:2]}{'*' * max(2, len(text) - 4)}{text[-2:]}"
    return "*" * len(text)
