"""Read-only venue cloud protocol preflight.

This verifies TLS, mTLS, request signing and response signing through the
status and short wait endpoints. It never pulls, acknowledges or mutates
venue business data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from services.venue_cloud_client import VenueCloudClient, validate_status_response


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only venue cloud signed protocol preflight")
    parser.add_argument("--wait-seconds", type=int, default=1, choices=range(1, 21))
    return parser.parse_args()


async def run(wait_seconds: int) -> dict[str, object]:
    client = VenueCloudClient()
    try:
        status = validate_status_response(await client.request_json("GET", "/api/internal/status"))
        wait = await client.wait_for_submissions(settings.VENUE_CLOUD_WORKER_ID, wait_seconds)
    finally:
        await client.close()
    return {
        "status_verified": True,
        "active_key_id_present": True,
        "pending_count": status["pending_count"],
        "wait_verified": True,
        "wait_available": wait["available"],
    }


def main() -> None:
    args = parse_args()
    print(json.dumps(asyncio.run(run(args.wait_seconds)), ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
