from __future__ import annotations

import re
from typing import Any


SENSITIVE = re.compile(r"(?:\b\d{17}[0-9Xx]\b|1[3-9]\d{9}|password|token|cookie|secret)", re.I)


def validate_fixture(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate only metadata; business values must be synthetic/de-identified."""
    serialized = repr(manifest)
    if SENSITIVE.search(serialized):
        raise ValueError("Staging fixture contains a sensitive value or credential-like field")
    required = ("run_id", "environment", "fictional_only", "production_data", "relations")
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ValueError(f"Staging fixture missing relation metadata: {', '.join(missing)}")
    if manifest["environment"] != "staging" or manifest["fictional_only"] is not True or manifest["production_data"] is not False:
        raise ValueError("fixture environment boundary failed")
    relations = manifest["relations"]
    if not isinstance(relations, dict) or any(int(value) < 0 for value in relations.values()):
        raise ValueError("fixture relation counts are invalid")
    return {"relation_counts": {str(key): int(value) for key, value in relations.items()}, "sensitive_matches": 0}
