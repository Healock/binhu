import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from routers import external_acquisition
from services.external_acquisition_jobs import (
    _has_incompatible_semantics,
    _kind_variants,
    _safe_result,
    _terminal_progress_message,
)
from services.external_acquisition_scheduler import (
    _code_summary_quality,
    _fetch_code_summary_sources,
)


def test_legacy_and_canonical_kinds_share_one_lease_family():
    assert _kind_variants("code_summary_fetch") == ["code_summary", "code_summary_fetch"]
    assert "visit_source_preview" in _kind_variants("visit_source")
    assert _has_incompatible_semantics("visit_source", "visit_source_preview")
    assert not _has_incompatible_semantics("code_summary", "code_summary_fetch")


def test_retry_rejects_preview_without_converting_it_to_auto_commit():
    async def scenario():
        with (
            patch.object(external_acquisition, "get_job_metadata", new=AsyncMock(return_value={
                "kind": "visit_source_preview", "status": "failed", "payload": {}, "schedule_id": None,
            })),
            patch.object(external_acquisition, "run_registered_source", new=AsyncMock()) as run,
            patch.object(external_acquisition, "record_admin_audit", new=AsyncMock()),
        ):
            with pytest.raises(HTTPException) as caught:
                await external_acquisition.retry_external_run(17, object(), {"id": 2, "permissions": ["visit.source.manage"]})
            assert caught.value.status_code == 409
            run.assert_not_awaited()

    asyncio.run(scenario())


def test_retry_only_accepts_failed_or_interrupted_runs():
    async def scenario():
        with patch.object(external_acquisition, "get_job_metadata", new=AsyncMock(return_value={
            "kind": "code_summary", "status": "success", "payload": {}, "schedule_id": None,
        })):
            with pytest.raises(HTTPException) as caught:
                await external_acquisition.retry_external_run(18, object(), {"id": 2, "permissions": ["visit.source.manage"]})
            assert caught.value.status_code == 409

    asyncio.run(scenario())


def test_schedule_listing_hides_sources_without_read_permission():
    async def scenario():
        schedules = [
            {"source_key": "visit_source", "revision": 1},
            {"source_key": "residence_full_scan", "revision": 1},
        ]
        with patch.object(external_acquisition, "list_schedules", new=AsyncMock(return_value=schedules)):
            result = await external_acquisition.external_schedules(
                {"id": 3, "permissions": ["visit.source.manage"]}, None
            )
        assert result["data"] == [schedules[0]]

    asyncio.run(scenario())


def test_code_summary_incomplete_quality_is_aggregate_only_and_survives_run_redaction():
    quality = _code_summary_quality({
        "peace": {
            "raw_count": 12,
            "valid_count": 10,
            "excluded_count": 1,
            "unclassified_count": 1,
            "invalid_time_count": 0,
            "location_counts": [{"display_name": "Sensitive location"}],
        },
        "manager": {
            "raw_count": 8,
            "valid_count": 7,
            "excluded_count": 1,
            "unclassified_count": 0,
            "invalid_time_count": 0,
        },
    })

    assert quality == {
        "manager": {
            "raw_count": 8,
            "valid_count": 7,
            "excluded_record_count": 1,
            "unclassified_location_count": 0,
            "invalid_time_count": 0,
        },
        "peace": {
            "raw_count": 12,
            "valid_count": 10,
            "excluded_record_count": 1,
            "unclassified_location_count": 1,
            "invalid_time_count": 0,
        },
    }
    assert _safe_result({"quality": quality}) == {"quality": quality}


def test_code_summary_total_source_timeout_is_bounded_and_classified():
    async def scenario():
        from config import settings
        from services.code_summary import CodeSummaryError

        async def slow_fetch(*_args):
            await asyncio.sleep(2)

        with (
            patch.object(settings, "CODE_SUMMARY_FETCH_TIMEOUT_SECONDS", 1),
            patch("services.code_summary.fetch_sources", new=slow_fetch),
            pytest.raises(CodeSummaryError) as captured,
        ):
            await _fetch_code_summary_sources(None, None, None)
        assert captured.value.code == "source_timeout"

    asyncio.run(scenario())


def test_failed_external_run_does_not_get_success_message():
    assert _terminal_progress_message("failed", None) == "未完成"
    assert _terminal_progress_message("success", None) == "已完成"
    assert _terminal_progress_message("failed", "来源不完整") == "来源不完整"
