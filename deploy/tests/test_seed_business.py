"""Contract tests for the isolated business-shadow seeder."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/kafka-shadow/seed_business.py"
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "load-tests"))
os.environ.setdefault("MYSQL_PASSWORD", "fictional-backend-password")
os.environ.setdefault("ENCRYPTION_KEY", "fictional-encryption-key")

spec = importlib.util.spec_from_file_location("seed_business", SCRIPT)
seed_business = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = seed_business
spec.loader.exec_module(seed_business)


def _environment(**overrides: str) -> dict[str, str]:
    result = {
        "APP_ENVIRONMENT": "shadow",
        "LOAD_TEST_RUN_ID": "KSHADOW-test-01",
        "MYSQL_HOST": "derived-mysql",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "shadow_backend_test",
        "MYSQL_PASSWORD": "fictional-backend-password",
        "MYSQL_ONLINE_DATA_DB": "KShadow_online_test",
        "MYSQL_ARCHIVE_DB": "KShadow_archive_test",
        "MYSQL_DAILY_REPORT_DB": "KShadow_daily_test",
    }
    result.update(overrides)
    return result


def test_validate_context_rejects_non_shadow_and_wrong_database_contract():
    with pytest.raises(seed_business.SeedSafetyError):
        seed_business.validate_context(
            "KSHADOW-test-01", _environment(APP_ENVIRONMENT="production")
        )
    with pytest.raises(seed_business.SeedSafetyError):
        seed_business.validate_context(
            "LT-test-01", _environment(LOAD_TEST_RUN_ID="LT-test-01")
        )
    with pytest.raises(seed_business.SeedSafetyError):
        seed_business.validate_context(
            "KSHADOW-test-01", _environment(MYSQL_HOST="mysql")
        )
    with pytest.raises(seed_business.SeedSafetyError):
        seed_business.validate_context(
            "KSHADOW-test-01", _environment(MYSQL_DAILY_REPORT_DB="KShadow_online_test")
        )


def test_validate_context_requires_cli_run_id_to_match_environment_exactly():
    with pytest.raises(seed_business.SeedSafetyError):
        seed_business.validate_context(
            "KSHADOW-other", _environment()
        )
    context = seed_business.validate_context("KSHADOW-test-01", _environment())
    assert context.run_id == "KSHADOW-test-01"
    assert context.host == "derived-mysql"


def test_marker_rows_must_be_exactly_one_matching_row_in_each_database():
    context = seed_business.validate_context("KSHADOW-test-01", _environment())
    matching = {
        context.online_db: [("shadow", context.run_id, context.online_db)],
        context.archive_db: [("shadow", context.run_id, context.archive_db)],
        context.daily_db: [("shadow", context.run_id, context.daily_db)],
    }
    seed_business.validate_marker_rows(context, matching)

    with pytest.raises(seed_business.SeedSafetyError):
        seed_business.validate_marker_rows(
            context,
            {**matching, context.daily_db: []},
        )
    with pytest.raises(seed_business.SeedSafetyError):
        seed_business.validate_marker_rows(
            context,
            {
                **matching,
                context.archive_db: [
                    ("shadow", context.run_id, "KShadow-wrong")
                ],
            },
        )


def test_fixture_shape_and_seed_batches_are_bounded():
    from fixture import make_tasks, make_users

    assert len(make_users()) == 76
    assert len(make_tasks()) == 3600
    batches = list(seed_business.batches(range(3600), 100))
    assert len(batches) == 36
    assert max(len(batch) for batch in batches) <= 100


def test_run_reuse_is_rejected_without_resetting_existing_fixture():
    seed_business.ensure_run_unused([] , "KSHADOW-test-01")
    with pytest.raises(seed_business.SeedSafetyError, match="already has fixture"):
        seed_business.ensure_run_unused(
            [("KSHADOW-test-01", 3600)], "KSHADOW-test-01"
        )


def test_business_seed_uses_canonical_local_sources_and_never_legacy_guard():
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'source_kind="local_table"' in source
    assert 'source_ref=""' in source
    assert "shadow:<run_id>" not in source
    assert "seed_shadow._guard" not in source
    assert "from seed_shadow" not in source
