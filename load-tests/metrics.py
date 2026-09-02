"""Small, dependency-free report helper for Locust CSV output."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def summarize(csv_path: Path) -> dict[str, object]:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8", newline="")))
    requests = sum(int(row.get("Request Count") or 0) for row in rows)
    failures = sum(int(row.get("Failure Count") or 0) for row in rows)
    return {
        "requests": requests,
        "failures": failures,
        "success_rate": round((requests - failures) / requests, 6) if requests else None,
        "rows": rows,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("用法：python metrics.py <locust_stats.csv>")
    print(json.dumps(summarize(Path(sys.argv[1])), ensure_ascii=False, indent=2))
