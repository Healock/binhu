"""Conservative redaction for logs, audit details, and diagnostic exports."""

import json
import re
from typing import Any


_SENSITIVE_KEYS = (
    r"password|passwd|pwd|token|secret|client_secret|access_token|"
    r"refresh_token|session_id"
)

_PATTERNS = [
    re.compile(
        rf"""(?ix)
        (["']?(?:{_SENSITIVE_KEYS}|authorization|cookie)["']?\s*[:=]\s*)
        (["'])
        .*?
        \2
        """,
    ),
    re.compile(
        r"""(?ix)
        (["']?cookie["']?\s*[:=]\s*)
        [^\r\n]+
        """,
    ),
    re.compile(
        r"""(?ix)
        (["']?authorization["']?\s*[:=]\s*(?:bearer\s+)?)
        [^\s,;}"']+
        """,
    ),
    re.compile(
        rf"""(?ix)
        (["']?(?:{_SENSITIVE_KEYS})["']?\s*[:=]\s*)
        [^\s,;}}"']+
        """,
    ),
    re.compile(
        r"(?i)([?&](?:access_token|refresh_token|client_secret)="
        r")[^&\s]+",
    ),
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/\-]+=*"),
]


def redact_text(value: str) -> str:
    redacted = value
    for pattern in _PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def sanitize_detail(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if any(
                marker in normalized
                for marker in (
                    "password",
                    "secret",
                    "token",
                    "cookie",
                    "authorization",
                    "session",
                )
            ):
                result[key] = "[REDACTED]"
            else:
                result[key] = sanitize_detail(item)
        return result
    if isinstance(value, list):
        return [sanitize_detail(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def sanitized_json(value: Any) -> str:
    return json.dumps(
        sanitize_detail(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )
