from datetime import datetime
from uuid import uuid4

import pytest

from services.kafka_aux_outbox_contract import (
    EventContractError,
    partition_key,
    photo_outbox_to_event,
    venue_outbox_to_event,
)


def test_photo_and_venue_contracts_are_separate_and_metadata_only():
    run_id = "KSHADOW-contract"
    photo = photo_outbox_to_event({
        "request_id": str(uuid4()), "source_id": 7, "work_order_id": 8,
        "action": "mark_completed", "timestamp": datetime(2026, 9, 7, 1, 2, 3),
    }, run_id=run_id)
    venue = venue_outbox_to_event({
        "request_id": str(uuid4()), "venue_id": 9, "config_revision": 2,
        "action": "update", "timestamp": datetime(2026, 9, 7, 1, 2, 3),
    }, run_id=run_id)
    assert photo["event_type"] != venue["event_type"]
    assert "phone" not in photo and "address" not in venue
    assert partition_key(photo) == b"photo:7:8"
    assert partition_key(venue) == b"venue:9"


def test_aux_contract_rejects_invalid_actions_and_missing_ids():
    base = {"request_id": str(uuid4()), "source_id": 1, "work_order_id": 2,
            "action": "unknown", "timestamp": datetime.now()}
    with pytest.raises(EventContractError):
        photo_outbox_to_event(base, run_id="KSHADOW-contract")
    with pytest.raises(EventContractError):
        venue_outbox_to_event({"request_id": str(uuid4()), "venue_id": 0,
                               "config_revision": 1, "action": "update",
                               "timestamp": datetime.now()}, run_id="KSHADOW-contract")
