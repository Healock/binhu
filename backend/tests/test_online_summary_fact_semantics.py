import pytest

from services.online_summary_read_model import (
    OnlineSummaryContractError,
    build_online_summary_fact,
    summary_fact_rebuild_reason,
)


def _fact(**overrides):
    value = {
        "event_id": "11111111-1111-1111-1111-111111111111",
        "command_id": "22222222-2222-2222-2222-222222222222",
        "operation_id": "33333333-3333-3333-3333-333333333333",
        "environment": "development",
        "run_id": "dev-summary-1",
        "task_id": "t_fullchain:1",
        "row_key": "a" * 32,
        "source_id": 1,
        "revision": 2,
        "task_revision": 2,
        "business_date": "2026-11-05",
        "parser_type": "全链条",
        "first_community": "社区甲",
        "first_inspector": "核查员甲",
        "task_state": "completed",
        "included": 1,
        "online_present": 1,
        "unable_to_verify": 0,
        "reached_bottom": 0,
        "effective_workload": 1,
        "activity_kind": "state_transition",
        "occurred_at": "2026-11-05T08:00:00Z",
        "secret": "summary-test-key-that-is-at-least-32-bytes",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("activity_kind", "deleted", "event_type"),
    [
        ("carryover", False, "summary.task.changed"),
        ("new", False, "summary.task.changed"),
        ("changed", False, "summary.task.changed"),
        ("state_transition", False, "summary.task.changed"),
        ("removed", True, "summary.task.deleted"),
    ],
)
def test_summary_fact_keeps_explicit_daily_activity_and_deletion_semantics(
    activity_kind, deleted, event_type
):
    workload = 1 if activity_kind in {"new", "state_transition"} else 0
    fact = build_online_summary_fact(
        **_fact(
            activity_kind=activity_kind,
            deleted=deleted,
            effective_workload=workload,
            online_present=0 if deleted else 1,
        )
    )

    assert fact["activity_kind"] == activity_kind
    assert fact["deleted"] is deleted
    assert fact["event_type"] == event_type
    assert fact["business_date"] == "2026-11-05"
    assert fact["task_revision"] == 2


def test_summary_fact_requires_first_ownership_instead_of_guessing_current_owner():
    with pytest.raises(OnlineSummaryContractError, match="first ownership"):
        build_online_summary_fact(**_fact(first_community=""))
    with pytest.raises(OnlineSummaryContractError, match="first ownership"):
        build_online_summary_fact(**_fact(first_inspector=""))


def test_summary_fact_has_only_hmac_directory_dimensions_and_no_sensitive_body():
    fact = build_online_summary_fact(**_fact())

    assert len(fact["community_token"]) == 64
    assert len(fact["inspector_token"]) == 64
    assert fact["community_token"] != "社区甲"
    assert fact["inspector_token"] != "核查员甲"
    assert not {"first_community", "first_inspector", "secret", "address", "name"} & fact.keys()


def test_summary_fact_hmac_dimensions_are_environment_isolated():
    development = build_online_summary_fact(**_fact())
    staging = build_online_summary_fact(
        **_fact(environment="staging", run_id="STG-20261105-01")
    )

    assert development["community_token"] != staging["community_token"]
    assert development["inspector_token"] != staging["inspector_token"]


def test_deleted_summary_fact_must_be_a_removed_activity():
    with pytest.raises(OnlineSummaryContractError, match="must be removed"):
        build_online_summary_fact(**_fact(
            deleted=True,
            activity_kind="changed",
            effective_workload=0,
            online_present=0,
        ))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("business_date", "2026/11/05"),
        ("parser_type", "bad\nparser"),
        ("effective_workload", 2),
        ("included", 2),
        ("environment", "staging"),
    ],
)
def test_summary_fact_rejects_invalid_date_parser_counters_and_environment(
    field, value
):
    with pytest.raises(OnlineSummaryContractError):
        build_online_summary_fact(**_fact(**{field: value}))


def test_summary_fact_hmacs_stable_row_key_and_normalizes_utc_timestamp():
    fact = build_online_summary_fact(**_fact(occurred_at="2026-11-05T16:00:00+08:00"))

    assert fact["row_key_hmac"] != "a" * 32
    assert len(fact["row_key_hmac"]) == 64
    assert fact["occurred_at"] == "2026-11-05T08:00:00Z"


def test_summary_fact_requires_snapshot_when_cross_day_activity_is_ambiguous():
    assert summary_fact_rebuild_reason(
        current_present=True,
        has_same_day_previous=False,
        has_historical_previous=True,
        revision_gap=False,
    ) == "cross_day_activity_requires_snapshot"
    assert summary_fact_rebuild_reason(
        current_present=True,
        has_same_day_previous=False,
        has_historical_previous=False,
        revision_gap=False,
    ) is None
    assert summary_fact_rebuild_reason(
        current_present=True,
        has_same_day_previous=True,
        has_historical_previous=True,
        revision_gap=True,
    ) == "revision_gap"
