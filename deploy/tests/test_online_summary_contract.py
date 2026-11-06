import pytest

from deploy.environments.event_pipeline.services.online_summary_contract import (
    OnlineSummaryContractError,
    summary_namespace,
    validate_summary_fact,
    watermark_record,
)
from deploy.environments.event_pipeline.services.online_summary_event import (
    summary_fact_from_bridge_event,
)
from deploy.environments.event_pipeline.services.kafka_envelope import (
    delivery_topic, serialize_event, validate_event,
)


def _fact(**overrides):
    value = {
        "contract_version": 1,
        "event_type": "summary.task.changed",
        "event_id": "11111111-1111-1111-1111-111111111111",
        "command_id": "22222222-2222-2222-2222-222222222222",
        "operation_id": "33333333-3333-3333-3333-333333333333",
        "environment": "development",
        "run_id": "dev-summary-1",
        "task_id": "t_fullchain:1",
        "source_id": 1,
        "revision": 2,
        "task_revision": 2,
        "business_date": "2026-11-05",
        "parser_type": "全链条",
        "row_key_hmac": "c" * 64,
        "community_token": "a" * 64,
        "inspector_token": "b" * 64,
        "task_state": "completed",
        "included": 1,
        "online_present": 1,
        "unable_to_verify": 0,
        "reached_bottom": 0,
        "effective_workload": 1,
        "activity_kind": "state_transition",
        "correction": False,
        "deleted": False,
        "occurred_at": "2026-11-05T00:00:00Z",
    }
    value.update(overrides)
    return value


def test_summary_fact_is_bounded_and_canonicalized():
    result = validate_summary_fact(_fact())
    assert result["business_date"] == "2026-11-05"
    assert result["community_token"] == "a" * 64
    assert "地址" not in result
    fractional = validate_summary_fact(_fact(occurred_at="2026-11-05T00:00:00.123456Z"))
    assert fractional["occurred_at"].endswith(".123456Z")


def test_summary_fact_routes_to_independent_kafka_topic_and_partition_key():
    fact = validate_summary_fact(_fact())
    assert delivery_topic(fact["event_type"], "events", 1, "development") == "dev.online-summary.facts.v1"
    assert delivery_topic(fact["event_type"], "dlq", 1, "staging") == "staging.online-summary.facts.dlq.v1"
    encoded = serialize_event(fact)
    assert b"summary.task.changed" in encoded
    assert validate_event(fact)["row_key_hmac"] == "c" * 64
    with pytest.raises(OnlineSummaryContractError):
        delivery_topic(fact["event_type"], "events", 4, "development")


@pytest.mark.parametrize("field,value", [
    ("business_date", "2026/11/05"),
    ("community_token", "社区一"),
    ("task_state", "unknown"),
    ("included", 2),
])
def test_summary_fact_rejects_unsafe_or_invalid_values(field, value):
    with pytest.raises(OnlineSummaryContractError):
        validate_summary_fact(_fact(**{field: value}))


def test_summary_fact_rejects_plaintext_dimensions():
    with pytest.raises(OnlineSummaryContractError):
        validate_summary_fact(_fact(display_name="张三"))


def test_summary_fact_rejects_inconsistent_delete_and_activity():
    with pytest.raises(OnlineSummaryContractError):
        validate_summary_fact(_fact(event_type="summary.task.deleted"))
    with pytest.raises(OnlineSummaryContractError):
        validate_summary_fact(_fact(activity_kind="guessed"))
    deleted = validate_summary_fact(_fact(
        event_type="summary.task.deleted", activity_kind="removed", deleted=True,
        included=0, online_present=0, effective_workload=0,
    ))
    assert deleted["deleted"] is True


def test_bridge_adapter_accepts_only_explicit_fact_and_fences_identity():
    assert summary_fact_from_bridge_event(
        {"event_type": "online.task.changed", "encrypted_payload": {"ciphertext": "opaque"}},
        environment="development", run_id="dev-summary-1",
    ) is None
    assert summary_fact_from_bridge_event(
        {"summary_fact": _fact()},
        environment="development", run_id="dev-summary-1",
    )["activity_kind"] == "state_transition"
    with pytest.raises(OnlineSummaryContractError):
        summary_fact_from_bridge_event(
            {"summary_fact": _fact()},
            environment="staging", run_id="STG-20261105-01",
        )


def test_namespace_and_watermark_are_environment_scoped():
    assert summary_namespace("development", "dev-summary-1").endswith(":development:dev-summary-1")
    watermark = watermark_record(
        environment="development", run_id="dev-summary-1", generation=1,
        complete_through_business_date="2026-11-05",
        kafka_offsets={"0": 4}, computed_at="2026-11-05T00:00:00Z",
    )
    assert watermark["kafka_offsets"] == {"0": 4}
    with pytest.raises(OnlineSummaryContractError):
        summary_namespace("production", "dev-summary-1")
