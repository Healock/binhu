import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from routers import external_acquisition
from services.external_acquisition_jobs import (
    _execute,
    _has_incompatible_semantics,
    _kind_variants,
    _safe_result,
    _terminal_progress_message,
    db_manager,
)
from services.external_acquisition_scheduler import (
    _code_summary_has_blocking_quality,
    _code_summary_quality,
    _fetch_code_summary_sources,
    _wait_certificate,
    run_registered_source,
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


def test_certificate_wait_allows_source_runs_longer_than_twelve_minutes():
    async def scenario():
        from services import external_acquisition_scheduler

        clock = iter(range(2000))
        calls = 0

        async def certificate_run(_run_id):
            nonlocal calls
            calls += 1
            status = "completed" if calls > 840 else "running"
            return {
                "status": status,
                "phase": "reading" if status == "running" else "finished",
                "current_page": min(calls, 92),
                "total_pages": 92,
            }

        context = SimpleNamespace(update=AsyncMock())
        with (
            patch.object(external_acquisition_scheduler.time, "monotonic", side_effect=lambda: next(clock)),
            patch("services.registry_certificate_jobs.get_certificate_source_run", new=AsyncMock(side_effect=certificate_run)),
            patch.object(external_acquisition_scheduler.asyncio, "sleep", new=AsyncMock()),
        ):
            result = await _wait_certificate(26, context)

        assert result["status"] == "success"
        assert result["certificate_run_id"] == 26
        assert calls == 841

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
            "out_of_range_count": 0,
        },
        "peace": {
            "raw_count": 12,
            "valid_count": 10,
            "excluded_record_count": 1,
            "unclassified_location_count": 1,
            "invalid_time_count": 0,
            "out_of_range_count": 0,
        },
    }
    assert _safe_result({"quality": quality}) == {"quality": quality}


def test_unclassified_locations_do_not_block_code_summary_but_invalid_records_do():
    assert not _code_summary_has_blocking_quality({
        "peace": {
            "excluded_record_count": 0,
            "unclassified_location_count": 44,
            "invalid_time_count": 0,
        },
    })
    assert _code_summary_has_blocking_quality({
        "peace": {
            "excluded_record_count": 1,
            "unclassified_location_count": 0,
            "invalid_time_count": 0,
        },
    })
    assert _code_summary_has_blocking_quality({
        "manager": {
            "excluded_record_count": 0,
            "unclassified_location_count": 0,
            "invalid_time_count": 1,
        },
    })


def test_code_summary_commits_when_only_locations_are_unclassified():
    async def scenario():
        class Pool:
            def acquire(self):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def begin(self):
                pass

            async def commit(self):
                self.committed = True

            async def rollback(self):
                self.rolled_back = True

        async def inline_job(_kind, _requested_by, _params, runner, **_kwargs):
            return await runner(SimpleNamespace(run_id=42, update=AsyncMock())), False

        aggregates = {
        "peace": {"raw_count": 45, "in_range_raw_count": 45, "valid_count": 45, "excluded_count": 0,
                      "unclassified_count": 44, "invalid_time_count": 0},
        "manager": {"raw_count": 3, "in_range_raw_count": 3, "valid_count": 3, "excluded_count": 0,
                        "unclassified_count": 0, "invalid_time_count": 0},
        }
        pool = Pool()
        with (
            patch("services.external_acquisition_scheduler._business_date", new=AsyncMock(return_value=date(2026, 11, 16))),
            patch("services.external_acquisition_scheduler._fetch_code_summary_sources", new=AsyncMock(return_value={
                "peace": {"rows": []}, "manager": {"rows": []},
            })),
            patch.object(db_manager, "get_pool", return_value=pool),
            patch("routers.code_summaries._directories", new=AsyncMock(return_value=(set(), set()))),
            patch("routers.code_summaries._manual_location_labels", new=AsyncMock(return_value={})),
            patch("services.code_summary.count_fullchain_registrations", new=AsyncMock(return_value={})),
            patch("services.code_summary.aggregate_rows", side_effect=lambda source, *_args, **_kwargs: aggregates[source]),
            patch("routers.code_summaries._commit_source", new=AsyncMock()) as commit_source,
            patch("services.external_acquisition_scheduler.create_job", new=AsyncMock(side_effect=inline_job)),
        ):
            result, reused = await run_registered_source("code_summary", 7, {
                "start_date": "2026-11-15", "end_date": "2026-11-15",
            })

        assert not reused
        assert result["status"] == "success"
        assert result["auto_commit_status"] == "committed"
        assert result["message"] == "汇总已提交，仍有地点待分类"
        assert result["quality"]["peace"]["unclassified_location_count"] == 44
        assert commit_source.await_count == 2
        assert pool.committed

    asyncio.run(scenario())


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


def test_code_summary_aggregation_error_is_classified_and_not_committed():
    async def scenario():
        from services.code_summary import CodeSummaryError

        class Pool:
            def acquire(self):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        async def inline_job(_kind, _requested_by, _params, runner, **_kwargs):
            result = await runner(SimpleNamespace(run_id=41, update=AsyncMock()))
            return result, False

        with (
            patch("services.external_acquisition_scheduler._business_date", new=AsyncMock(return_value=date(2026, 11, 16))),
            patch("services.external_acquisition_scheduler._fetch_code_summary_sources", new=AsyncMock(return_value={
                "peace": {"rows": [{"private": "source row"}]}, "manager": {"rows": []},
            })),
            patch.object(db_manager, "get_pool", return_value=Pool()),
            patch("routers.code_summaries._directories", new=AsyncMock(return_value=(set(), set()))),
            patch("routers.code_summaries._manual_location_labels", new=AsyncMock(return_value={})),
            patch("services.code_summary.count_fullchain_registrations", new=AsyncMock(return_value={})),
            patch("services.code_summary.aggregate_rows", side_effect=CodeSummaryError(
                "scope_mismatch", "must not expose source row"
            )),
            patch("services.external_acquisition_scheduler.create_job", new=AsyncMock(side_effect=inline_job)),
        ):
            result, reused = await run_registered_source(
                "code_summary", 7,
                {"start_date": "2026-11-10", "end_date": "2026-11-11"},
            )

        assert not reused
        assert result["status"] == "failed"
        assert result["auto_commit_status"] == "skipped"
        assert result["error_code"] == "scope_mismatch"
        assert result["safety_gate_code"] == "scope_mismatch"
        assert result["message"] == "平安码来源数据校验未通过，自动提交已跳过，现有汇总保持不变"
        assert "must not expose" not in result["message"]

    asyncio.run(scenario())


def test_external_job_persists_safe_runner_error_code():
    async def scenario():
        class Cursor:
            def __init__(self, statements):
                self.statements = statements

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def execute(self, sql, params=()):
                self.statements.append((sql, params))

            async def fetchone(self):
                return (None,)

        class Connection:
            def __init__(self, statements):
                self.statements = statements

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def cursor(self):
                return Cursor(self.statements)

        class Pool:
            def __init__(self):
                self.statements = []

            def acquire(self):
                return Connection(self.statements)

        pool = Pool()

        async def runner(_ctx):
            return {
                "status": "failed", "auto_commit_status": "skipped",
                "error_code": "scope_mismatch", "safety_gate_code": "scope_mismatch",
                "message": "来源校验未通过，现有汇总保持不变",
                "raw_rows": [{"private": "must not persist"}],
            }

        with patch.object(db_manager, "get_pool", return_value=pool):
            await _execute(41, runner)

        terminal = next(params for sql, params in pool.statements if "phase='finished'" in sql)
        assert terminal[3] == "scope_mismatch"
        assert terminal[5] == "scope_mismatch"
        assert "must not persist" not in terminal[1]

    asyncio.run(scenario())


def test_failed_external_run_does_not_get_success_message():
    assert _terminal_progress_message("failed", None) == "未完成"
    assert _terminal_progress_message("success", None) == "已完成"
    assert _terminal_progress_message("failed", "来源不完整") == "来源不完整"
