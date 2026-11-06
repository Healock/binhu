from datetime import datetime

import pytest

from services.online_summary_read_model import (
    OnlineSummaryContractError,
    OnlineSummaryReadModelError,
    SummaryAggregate,
    SummaryIdentity,
    decode_watermark,
    encode_watermark,
    require_online_summary_ready,
    summary_aggregate_key,
    summary_dimension_token,
    summary_index_key,
    summary_state_key,
    summary_watermark_key,
)


def _identity():
    return SummaryIdentity("development", "dev-online-summary-1")


def _aggregate():
    token = "a" * 64
    return SummaryAggregate(
        business_date="2026-11-05", parser_type="全链条",
        community_token=token, inspector_token="b" * 64,
        data_total=10, unchecked=5, checked=2, completed=3,
        unable_to_verify=1, effective_workload=3, revision=4,
    )


def test_keys_are_environment_and_run_scoped_without_plain_dimensions():
    identity = _identity()
    aggregate = _aggregate()
    key = summary_aggregate_key(identity, aggregate)
    assert key.startswith("binhu:online-summary:v1:development:dev-online-summary-1:")
    assert "社区" not in key
    assert summary_index_key(identity, business_date="2026-11-05", parser_type="全链条").endswith(":2026-11-05:全链条")
    assert summary_state_key(identity).endswith(":state")
    assert summary_watermark_key(identity).endswith(":watermark")


def test_dimension_tokens_are_stable_and_scoped():
    first = summary_dimension_token("社区一", dimension="community", environment="development", secret="x" * 32)
    second = summary_dimension_token("社区一", dimension="community", environment="development", secret="x" * 32)
    other = summary_dimension_token("社区一", dimension="community", environment="staging", secret="x" * 32)
    assert first == second and first != other and len(first) == 64


def test_aggregate_rejects_inconsistent_counts():
    with pytest.raises(OnlineSummaryContractError):
        SummaryAggregate(
            business_date="2026-11-05", parser_type="全链条",
            community_token="a" * 64, inspector_token="b" * 64,
            data_total=1, unchecked=1, checked=1, completed=0,
            unable_to_verify=0, effective_workload=0, revision=1,
        )


def test_watermark_round_trip_and_identity_fence():
    identity = _identity()
    encoded = encode_watermark(
        identity=identity, generation=1,
        complete_through_business_date="2026-11-05", source_revision=8,
        kafka_offsets={"0": 4},
        computed_at="2026-11-05T08:00:00Z",
    )
    decoded = decode_watermark(encoded, identity)
    assert decoded["source_revision"] == 8
    with pytest.raises(OnlineSummaryReadModelError):
        decode_watermark(encoded, SummaryIdentity("staging", "STG-20261105-01"))


@pytest.mark.asyncio
async def test_ready_reader_fails_closed_on_missing_state():
    class Redis:
        async def get(self, _key):
            return "rebuilding"

        async def hgetall(self, _key):
            return {}

    with pytest.raises(OnlineSummaryReadModelError, match="projection_pending"):
        await require_online_summary_ready(Redis(), _identity())
