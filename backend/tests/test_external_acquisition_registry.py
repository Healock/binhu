from datetime import datetime, timezone

import pytest

from services.external_acquisition_registry import (
    SOURCE_DEFINITIONS,
    can_access_source,
    next_run,
    sanitize_source_params,
    source_definition,
    validate_timezone,
)
from services.external_acquisition_scheduler import (
    _code_summary_quality,
    _code_summary_range,
    _scheduled_date_range,
)


def test_catalog_has_independent_default_disabled_sources():
    keys = {item.key for item in SOURCE_DEFINITIONS}
    assert {"registry_certificate", "residence_full_scan", "qmf_source", "visit_source", "code_summary", "venue_cloud"} <= keys
    assert all(item.supports_manual for item in SOURCE_DEFINITIONS if item.key != "txdocs_monitor")
    assert not source_definition("txdocs_monitor").supports_manual
    assert source_definition("code_summary_fetch").key == "code_summary"
    assert source_definition("visit_source_preview").key == "visit_source"


def test_cron_next_run_uses_business_timezone_and_utc_storage():
    base = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)
    assert next_run("0 2 * * *", "Asia/Shanghai", base) == datetime(2026, 10, 25, 18, 0)


@pytest.mark.parametrize("expression", ["", "0 2 * *", "0 2 * * * *", "bad cron"])
def test_cron_requires_standard_five_fields(expression):
    with pytest.raises(ValueError):
        next_run(expression, "Asia/Shanghai")


def test_invalid_timezone_is_rejected():
    with pytest.raises(ValueError):
        validate_timezone("Not/AZone")


def test_super_admin_and_permission_access_are_separate():
    source = source_definition("qmf_source")
    assert can_access_source({"role": "super_admin", "permissions": []}, source)
    assert can_access_source({"permissions": ["sync.trigger"]}, source)
    assert not can_access_source({"permissions": []}, source)


def test_schedule_params_reject_credentials_and_unknown_business_fields():
    assert sanitize_source_params("visit_source", {"start_date": "2026-10-01", "end_date": "2026-10-02"})["start_date"] == "2026-10-01"
    with pytest.raises(ValueError):
        sanitize_source_params("visit_source", {"password": "secret"})
    with pytest.raises(ValueError):
        sanitize_source_params("visit_source", {"address": "不应保存"})


@pytest.mark.parametrize(
    "days_ago, expected",
    [
        (0, ("2026-10-18", "2026-10-18")),
        (1, ("2026-10-17", "2026-10-17")),
        (2, ("2026-10-16", "2026-10-17")),
    ],
)
def test_code_summary_schedule_supports_relative_business_date_windows(days_ago, expected):
    start, end = _code_summary_range({"days_ago": days_ago}, datetime(2026, 10, 18).date())
    assert (start.isoformat(), end.isoformat()) == expected


@pytest.mark.parametrize(
    "days_ago, expected",
    [
        (0, ("2026-10-18", "2026-10-18")),
        (1, ("2026-10-17", "2026-10-17")),
        (2, ("2026-10-16", "2026-10-17")),
        (7, ("2026-10-11", "2026-10-17")),
    ],
)
def test_visit_schedule_supports_relative_business_date_windows(days_ago, expected):
    start, end = _scheduled_date_range({"days_ago": days_ago}, datetime(2026, 10, 18).date())
    assert (start.isoformat(), end.isoformat()) == expected


def test_code_summary_relative_date_overrides_legacy_fixed_dates():
    start, end = _code_summary_range(
        {"days_ago": 0, "start_date": "2026-10-12", "end_date": "2026-10-13"},
        datetime(2026, 10, 18).date(),
    )
    assert (start.isoformat(), end.isoformat()) == ("2026-10-18", "2026-10-18")


def test_code_summary_default_target_remains_previous_business_date():
    start, end = _code_summary_range({}, datetime(2026, 10, 18).date())
    assert (start.isoformat(), end.isoformat()) == ("2026-10-17", "2026-10-17")


def test_code_summary_keeps_legacy_fixed_date_params_when_no_offset_is_saved():
    start, end = _code_summary_range(
        {"start_date": "2026-10-12", "end_date": "2026-10-13"},
        datetime(2026, 10, 18).date(),
    )
    assert (start.isoformat(), end.isoformat()) == ("2026-10-12", "2026-10-13")


def test_code_summary_range_rejects_reversed_legacy_dates():
    with pytest.raises(ValueError):
        _code_summary_range(
            {"start_date": "2026-10-13", "end_date": "2026-10-12"},
            datetime(2026, 10, 18).date(),
        )


def test_schedule_quality_exposes_only_out_of_range_count():
    quality = _code_summary_quality({
        "manager": {
            "raw_count": 3,
            "valid_count": 1,
            "out_of_range_count": 2,
            "rows": [{"address": "private", "name": "private"}],
        },
    })
    assert quality == {
        "manager": {
            "raw_count": 3,
            "valid_count": 1,
            "excluded_record_count": 0,
            "unclassified_location_count": 0,
            "invalid_time_count": 0,
            "out_of_range_count": 2,
        },
    }


@pytest.mark.parametrize("value", [-1, 32, True, "2"])
@pytest.mark.parametrize("source_key", ["code_summary", "visit_source"])
def test_schedule_days_ago_is_bounded_integer_for_window_sources(source_key, value):
    with pytest.raises(ValueError):
        sanitize_source_params(source_key, {"days_ago": value})


def test_days_ago_is_not_accepted_for_other_sources():
    with pytest.raises(ValueError):
        sanitize_source_params("venue_cloud", {"days_ago": 1})


def test_visit_schedule_accepts_days_ago_and_preserves_legacy_date_parameters():
    assert sanitize_source_params("visit_source", {"days_ago": 2}) == {"days_ago": 2}
    assert sanitize_source_params("visit_source", {
        "start_date": "2026-10-12",
        "end_date": "2026-10-13",
    }) == {"start_date": "2026-10-12", "end_date": "2026-10-13"}
