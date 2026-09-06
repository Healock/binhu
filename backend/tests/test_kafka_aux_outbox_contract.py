from datetime import datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from services.kafka_aux_outbox_contract import (
    EventContractError,
    partition_key,
    photo_outbox_to_event,
    serialize_aux_event,
    validate_aux_event,
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


def test_photo_outbox_supports_persisted_actions_and_derives_stable_event_id():
    row = {
        "outbox_id": 123,
        "source_id": 7,
        "work_order_id": 8,
        "action": "append_request",
        "timestamp": datetime(2026, 9, 7, 1, 2, 3),
    }
    first = photo_outbox_to_event(row, run_id="KSHADOW-contract")
    second = photo_outbox_to_event(row, run_id="KSHADOW-contract")

    assert first == second
    assert first["event_id"] == str(uuid5(
        NAMESPACE_URL, "KSHADOW-contract/photo_sheet_outbox/123"
    ))
    assert first["event_id"] != str(row["outbox_id"])
    assert first["action"] == "append_request"

    row["action"] = "mark_completed"
    assert photo_outbox_to_event(row, run_id="KSHADOW-contract")["action"] == "mark_completed"


def test_aux_event_validation_is_strict_and_serialization_is_canonical():
    event = photo_outbox_to_event({
        "request_id": str(uuid4()),
        "source_id": 7,
        "work_order_id": 8,
        "action": "mark_completed",
        "timestamp": datetime(2026, 9, 7, 1, 2, 3),
    }, run_id="KSHADOW-contract")
    normalized = validate_aux_event(event)
    assert normalized == event
    assert serialize_aux_event(event) == serialize_aux_event(dict(reversed(event.items())))

    with pytest.raises(EventContractError):
        validate_aux_event({**event, "ignored": 1})
    with pytest.raises(EventContractError):
        validate_aux_event({**event, "timestamp": "2026-09-07T01:02:03+00:00"})
    with pytest.raises(EventContractError):
        validate_aux_event({**event, "run_id": "KSHADOW-"})
    with pytest.raises(EventContractError):
        validate_aux_event({**event, "source_id": 2**63})
    with pytest.raises(EventContractError):
        validate_aux_event({**event, "event_type": []})
    with pytest.raises(EventContractError):
        validate_aux_event({**event, "action": []})


def test_aux_contract_rejects_invalid_actions_and_missing_ids():
    base = {"request_id": str(uuid4()), "source_id": 1, "work_order_id": 2,
            "action": "unknown", "timestamp": datetime.now()}
    with pytest.raises(EventContractError):
        photo_outbox_to_event(base, run_id="KSHADOW-contract")
    with pytest.raises(EventContractError):
        venue_outbox_to_event({"request_id": str(uuid4()), "venue_id": 0,
                               "config_revision": 1, "action": "update",
                               "timestamp": datetime.now()}, run_id="KSHADOW-contract")
    with pytest.raises(EventContractError):
        photo_outbox_to_event({**base, "action": "sync_photo"}, run_id="KSHADOW-contract")
    with pytest.raises(EventContractError):
        photo_outbox_to_event({k: v for k, v in base.items() if k != "request_id"}, run_id="KSHADOW-contract")
