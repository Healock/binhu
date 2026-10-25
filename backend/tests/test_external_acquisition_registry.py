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
