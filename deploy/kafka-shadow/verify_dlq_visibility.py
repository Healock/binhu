"""Verify a known DLQ event is visible through a fresh consumer group.

The verifier waits for assignment before polling.  This avoids mistaking a
slow group coordinator for a missing DLQ record.  It only reads synthetic
metadata and never acknowledges or mutates the topic.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from aiokafka import AIOKafkaConsumer

from relay_runtime import configuration


async def verify(event_id: str) -> dict:
    config = configuration()
    consumer = AIOKafkaConsumer(
        "binhu.task.events.dlq.v1",
        bootstrap_servers=config["bootstrap"],
        group_id="dlq-verify-" + uuid.uuid4().hex,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    try:
        await consumer.start()
        deadline = asyncio.get_running_loop().time() + 30
        while not consumer.assignment() and asyncio.get_running_loop().time() < deadline:
            await consumer.getmany(timeout_ms=500, max_records=1)
        if not consumer.assignment():
            raise RuntimeError("dlq consumer assignment timeout")
        while asyncio.get_running_loop().time() < deadline:
            batches = await consumer.getmany(timeout_ms=1000, max_records=100)
            for records in batches.values():
                for record in records:
                    payload = json.loads(record.value)
                    if payload.get("event_id") == event_id:
                        return {
                            "status": "passed",
                            "event_id": event_id,
                            "partition": record.partition,
                            "offset": record.offset,
                        }
        raise RuntimeError("dlq event not observed")
    finally:
        await consumer.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("event_id")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(args.event_id)), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
