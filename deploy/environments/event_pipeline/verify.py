"""Synthetic Dev delivery acceptance; requires the prepared database marker."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
import re

from .runtime import configuration, connect
from .services.kafka_delivery_store import enqueue_delivery
from .services.derived_revision_cache import RevisionCache


_NONCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def acceptance_nonce() -> str:
    """Return a caller supplied nonce so a rerun cannot reuse an old ledger ID."""
    nonce = os.environ.get("DEV_ACCEPT_NONCE", "legacy")
    if not _NONCE.fullmatch(nonce):
        raise ValueError("invalid acceptance nonce")
    return nonce


def acceptance_revision() -> int:
    """Return the revision range for a run, allowing recovery above savepoint state."""
    raw = os.environ.get("DEV_ACCEPT_BASE_REVISION", "1")
    try:
        base = int(raw)
    except ValueError:
        raise ValueError("invalid acceptance base revision") from None
    if base < 1 or base >= 2**63 - 3:
        raise ValueError("invalid acceptance base revision")
    return base


def fixture(run_id, revision, *, nonce="legacy"):
    # Reserved synthetic reference; no production task or person is inspected.
    task_id = "t_fullchain:900000000000000001"
    return {"schema_version": 1, "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}/{nonce}/{revision}")),
            "event_type": "task.saved", "task_id": task_id, "source_id": 900000000000000001,
            "revision": revision, "operation_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, run_id)),
            "changed_fields": ["task_state"], "timestamp": "2026-09-10T00:00:00Z",
            "environment": "development", "run_id": run_id}


def acceptance_event_ids(run_id, base_revision, nonce):
    """Return the three unique delivery IDs for one acceptance attempt.

    A run can intentionally contain older attempts.  Verification must scope
    delivery counts to this attempt instead of treating every historical row
    for the run as part of the current result.
    """
    target_revision = base_revision + 2
    return tuple(
        fixture(run_id, revision, nonce=nonce)["event_id"]
        for revision in (base_revision, target_revision, base_revision + 1)
    )


async def run(mode):
    config = configuration()
    pool = await connect(config)
    try:
        nonce = acceptance_nonce()
        base_revision = acceptance_revision()
        target_revision = base_revision + 2
        if mode == "seed":
            async with pool.acquire() as conn:
                try:
                    await conn.begin()
                    async with conn.cursor() as cur:
                        # Duplicate enqueue plus out-of-order revisions.
                        for revision in (base_revision, target_revision, base_revision + 1, target_revision):
                            await enqueue_delivery(cur, fixture(config["DEV_RUN_ID"], revision, nonce=nonce), run_id=config["DEV_RUN_ID"])
                    await conn.commit()
                except BaseException:
                    await conn.rollback()
                    raise
            return {"environment": "development", "run_id": config["DEV_RUN_ID"], "unique_events": 3, "enqueued": True}
        from redis.asyncio import Redis
        client = Redis(host=config["REDIS_HOST"], password=config["REDIS_PASSWORD"],
                       decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        try:
            if mode == "business":
                event_id = os.environ.get("DEV_ACCEPT_EVENT_ID", "")
                task_id = os.environ.get("DEV_ACCEPT_TASK_ID", "")
                source_id = int(os.environ.get("DEV_ACCEPT_SOURCE_ID", "0"))
                revision = int(os.environ.get("DEV_ACCEPT_REVISION", "0"))
                expected = {"event_id": event_id, "task_id": task_id,
                            "source_id": source_id, "revision": revision}
                if not event_id or not task_id or source_id <= 0 or revision <= 0:
                    raise ValueError("business acceptance inputs required")
            else:
                expected = fixture(config["DEV_RUN_ID"], target_revision, nonce=nonce)
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT revision FROM dev_task_revisions WHERE run_id=%s AND task_id=%s AND source_id=%s",
                                      (config["DEV_RUN_ID"], expected["task_id"], expected["source_id"]))
                    row = await cur.fetchone()
                    await cur.execute("SELECT status FROM _kafka_event_delivery WHERE run_id=%s AND event_id=%s",
                                      (config["DEV_RUN_ID"], expected["event_id"]))
                    delivery = await cur.fetchone()
                    if mode != "business":
                        event_ids = acceptance_event_ids(config["DEV_RUN_ID"], base_revision, nonce)
                        await cur.execute(
                            "SELECT status,COUNT(*) FROM _kafka_event_delivery "
                            "WHERE run_id=%s AND event_id IN (%s,%s,%s) GROUP BY status",
                            (config["DEV_RUN_ID"], *event_ids),
                        )
                        states = dict(await cur.fetchall())
                    else:
                        states = {delivery[0]: 1} if delivery else {}
            cache = RevisionCache(client, config["DEV_RUN_ID"])
            if mode == "business":
                value = await cache.get(expected["task_id"], expected["source_id"], "flink-dev", expected["revision"])
                passed = row == (expected["revision"],) and delivery == ("published",) and value is not None
                report = {"environment": "development", "run_id": config["DEV_RUN_ID"],
                          "business_event_id": expected["event_id"], "delivery_published": delivery == ("published",),
                          "mysql_revision_correct": row == (expected["revision"],),
                          "redis_revision_correct": value is not None,
                          "business_integration_verified": passed}
            else:
                value = await cache.get(expected["task_id"], expected["source_id"], "flink-dev", target_revision)
                passed = row == (target_revision,) and value is not None and states == {"published": 3}
                report = {"environment": "development", "run_id": config["DEV_RUN_ID"],
                          "mysql_revision_correct": row == (target_revision,), "redis_revision_correct": value is not None,
                          "delivery_counts": states, "minimal_event_flow_passed": passed,
                          "checkpoint_recovery_verified": False, "business_integration_verified": False}
            if not passed:
                raise ValueError("Dev event flow not converged")
            return report
        finally:
            await client.aclose()
    finally:
        pool.close()
        await pool.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "verify", "business"))
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args.mode))))
    except Exception:
        raise SystemExit("Dev event acceptance failed; preserve private diagnostics") from None
