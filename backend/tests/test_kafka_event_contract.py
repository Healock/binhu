"""Tests for the isolated Kafka shadow task metadata contract."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # pragma: no cover - requirements-test stays lightweight
    Draft202012Validator = None
    FormatChecker = None

from services.kafka_event_contract import (
    MAX_EVENT_BYTES,
    SOURCE_TABLE_NAMES,
    canonical_json,
    encode_event,
    partition_key,
    partition_key_text,
    serialize_task_event,
    validate_event,
    validate_task_event,
)


def _event(**overrides):
    event = {
        "schema_version": 1,
        "event_id": "123e4567-e89b-12d3-a456-426614174000",
        "event_type": "task.saved",
        "task_id": "t_fullchain:27",
        "source_id": 27,
        "revision": 8,
        "operation_id": "123e4567-e89b-12d3-a456-426614174001",
        "changed_fields": ["task_state", "address"],
        "timestamp": "2026-09-06T08:49:57.123Z",
        "environment": "shadow",
        "run_id": "KSHADOW-20260906T084957Z-fcbad2",
    }
    event.update(overrides)
    return event


def test_valid_event_is_metadata_only_and_serializes_canonically():
    event = validate_task_event(_event())

    assert event["changed_fields"] == ["address", "task_state"]
    assert validate_event(_event()) == event
    assert "body" not in event
    assert "values" not in event
    assert json.loads(serialize_task_event(event)) == event
    assert json.loads(encode_event(event)) == event
    assert len(serialize_task_event(event)) <= MAX_EVENT_BYTES

    reordered = dict(reversed(list(_event().items())))
    assert canonical_json(event) == canonical_json(reordered)


@pytest.mark.parametrize(
    "extra",
    [
        {"body": "身份证号=FICTIONAL"},
        {"values": {"address": "敏感正文"}},
        {"error": {"message": "arbitrary detail"}},
        {"sensitive_field": "手机号=FICTIONAL"},
    ],
)
def test_unknown_body_nested_and_free_form_properties_are_rejected(extra):
    with pytest.raises(ValueError):
        validate_task_event({**_event(), **extra})


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", "123e4567-e89b-12d3-a456-42661417400z"),
        ("operation_id", "123e4567-e89b-12d3-a456-42661417400"),
        ("event_id", str(uuid.uuid4()).replace("-", "")),
    ],
)
def test_uuid_fields_require_canonical_uuid_strings(field, value):
    with pytest.raises(ValueError):
        validate_task_event(_event(**{field: value}))


@pytest.mark.parametrize("field", ["source_id", "revision"])
def test_integer_fields_reject_bool(field):
    with pytest.raises(ValueError):
        validate_task_event(_event(**{field: True}))


def test_revision_rejects_negative_and_overlarge_values():
    for value in (-1, 2**63):
        with pytest.raises(ValueError):
            validate_task_event(_event(revision=value))


def test_parser_table_allowlist_is_fixed_and_matches_registry():
    from services.parsers import TABLE_NAMES

    assert SOURCE_TABLE_NAMES == frozenset(TABLE_NAMES.values())


def test_oversized_source_reference_is_rejected_without_echoing_input():
    value = "t_fullchain:" + "9" * 10_000

    with pytest.raises(ValueError) as error:
        validate_task_event(_event(task_id=value))

    assert value not in str(error.value)


def test_changed_fields_are_a_closed_english_allowlist_of_scalars():
    for changed_fields in (
        ["身份证号"],
        ["address=value"],
        [{"field": "address"}],
        ["unknown_field"],
        ["address", "address"],
    ):
        with pytest.raises(ValueError):
            validate_task_event(_event(changed_fields=changed_fields))


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "t_unknown:27"),
        ("task_id", "t_fullchain:0"),
        ("task_id", "t_fullchain:27:nested"),
        ("timestamp", "2026-09-06T08:49:57+08:00"),
        ("timestamp", "2026-09-06 08:49:57Z"),
        ("environment", "production"),
        ("run_id", "LT-20260906-01"),
    ],
)
def test_source_timestamp_environment_and_run_id_are_strict(field, value):
    with pytest.raises(ValueError):
        validate_task_event(_event(**{field: value}))


def test_partition_key_is_stable_for_same_task_and_source_pair():
    first = partition_key(_event(revision=1, operation_id="123e4567-e89b-12d3-a456-426614174001"))
    second = partition_key(_event(revision=9, operation_id="123e4567-e89b-12d3-a456-426614174002"))

    assert first == second
    assert isinstance(first, bytes)
    assert partition_key_text(_event()) == "t_fullchain:27|27"
    assert first != partition_key(_event(source_id=28))
    assert first != partition_key(_event(task_id="t_rental_check:27"))


def test_schema_file_is_the_new_strict_kafka_contract():
    schema = json.loads(
        (Path(__file__).parents[2] / "deploy/kafka-shadow/task-event-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["$id"].endswith("task-event-v1.schema.json")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["environment"]["const"] == "shadow"
    assert "task.saved" in schema["properties"]["event_type"]["enum"]
    assert "task_id" in schema["required"]
    assert schema["properties"]["task_id"]["maxLength"] == max(
        len(table) + 1 + 19 for table in SOURCE_TABLE_NAMES
    )
    expected_table_pattern = "^(?:" + "|".join(sorted(SOURCE_TABLE_NAMES)) + "):"
    assert schema["properties"]["task_id"]["pattern"] == expected_table_pattern


def test_python_and_jsonschema_agree_on_the_same_valid_and_invalid_samples():
    """The executable validator and published schema reject the same edges."""
    if Draft202012Validator is None:
        pytest.skip("jsonschema is not installed in this lightweight test environment")

    schema = json.loads(
        (Path(__file__).parents[2] / "deploy/kafka-shadow/task-event-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.check_schema(schema)

    valid_samples = [
        _event(),
        _event(
            event_type="task.deleted",
            task_id="t_delivery_industry:9223372036854775807",
            source_id=9223372036854775807,
            revision=0,
            changed_fields=[],
            timestamp="2026-09-06T08:49:57Z",
        ),
    ]
    for sample in valid_samples:
        normalized = validate_task_event(sample)
        assert validator.is_valid(normalized), list(validator.iter_errors(normalized))

    invalid_samples = [
        _event(body="FICTIONAL-SENSITIVE-VALUE"),
        _event(values={"address": "FICTIONAL-SENSITIVE-VALUE"}),
        _event(source_id=True),
        _event(revision=True),
        _event(event_id="123e4567-e89b-12d3-a456-42661417400z"),
        _event(operation_id="123e4567-e89b-12d3-a456-42661417400"),
        _event(changed_fields=["unknown_field"]),
        _event(task_id="t_fullchain:" + "9" * 10_000),
        _event(task_id="t_fullchain:9223372036854775808"),
        _event(timestamp="2026-09-06T08:49:57+08:00"),
        _event(run_id="LT-20260906-01"),
    ]
    for sample in invalid_samples:
        with pytest.raises(ValueError) as error:
            validate_task_event(sample)
        assert all(
            str(value) not in str(error.value)
            for value in sample.values()
            if isinstance(value, (str, int, float, bool))
        )
        assert not validator.is_valid(sample)
