from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import settings
from migrations.domain_migration import DOMAIN_TABLES, quote_identifier
from services.domain_routing import rewrite_domain_sql


def test_domain_sql_is_unchanged_before_switch():
    with patch.object(settings, "PLATFORM_DOMAIN_ACTIVE", False):
        assert rewrite_domain_sql("SELECT * FROM _users") == "SELECT * FROM _users"


def test_domain_sql_routes_unqualified_and_legacy_qualified_tables():
    with patch.object(settings, "PLATFORM_DOMAIN_ACTIVE", True), patch.object(
        settings, "VISIT_DOMAIN_ACTIVE", True
    ):
        sql = rewrite_domain_sql(
            "SELECT user.id FROM OnlineData._users user "
            "JOIN _communities community ON community.id=user.id "
            "JOIN t_visit_details visit ON visit.`社区`=community.name"
        )
    assert "`PlatformData`.`_users`" in sql
    assert "`PlatformData`.`_communities`" in sql
    assert "`VisitData`.`t_visit_details`" in sql


def test_domain_sql_routes_historical_online_schema_in_shadow_database():
    with patch.object(settings, "MYSQL_ONLINE_DATA_DB", "LoadTest_LT_20260902_01"), patch.object(
        settings, "PLATFORM_DOMAIN_ACTIVE", True
    ):
        sql = rewrite_domain_sql(
            "SELECT config_value FROM OnlineData._system_config "
            "WHERE config_key = 'timezone'"
        )
    assert sql == (
        "SELECT config_value FROM `PlatformData`.`_system_config` "
        "WHERE config_key = 'timezone'"
    )


def test_qmf_registration_runs_are_stored_in_platform_domain():
    with patch.object(settings, "PLATFORM_DOMAIN_ACTIVE", True):
        sql = rewrite_domain_sql(
            "SELECT * FROM _qmf_registration_runs WHERE id=%s"
        )
    assert "`PlatformData`.`_qmf_registration_runs`" in sql


def test_qmf_feedback_scan_snapshots_remain_in_online_data_domain():
    with patch.object(settings, "PLATFORM_DOMAIN_ACTIVE", True):
        sql = rewrite_domain_sql(
            "SELECT * FROM _qmf_status_snapshots WHERE row_key=%s"
        )
    assert "PlatformData" not in sql
    assert "_qmf_status_snapshots" in sql


def test_code_summary_tables_are_stored_in_visit_domain():
    with patch.object(settings, "VISIT_DOMAIN_ACTIVE", True):
        sql = rewrite_domain_sql(
            "SELECT * FROM _code_summary_runs run "
            "JOIN _code_daily_snapshots snapshot ON snapshot.run_id=run.id "
            "JOIN _code_summary_location_counts counts ON counts.run_id=run.id "
            "JOIN _code_summary_location_labels labels ON labels.location_key=counts.location_key"
        )
    assert "`VisitData`.`_code_summary_runs`" in sql
    assert "`VisitData`.`_code_daily_snapshots`" in sql
    assert "`VisitData`.`_code_summary_location_counts`" in sql
    assert "`VisitData`.`_code_summary_location_labels`" in sql


def test_administrative_areas_are_stored_in_platform_domain():
    with patch.object(settings, "PLATFORM_DOMAIN_ACTIVE", True):
        sql = rewrite_domain_sql(
            "SELECT * FROM _administrative_areas WHERE code=%s"
        )
    assert "`PlatformData`.`_administrative_areas`" in sql


def test_help_documents_are_stored_in_platform_domain():
    with patch.object(settings, "PLATFORM_DOMAIN_ACTIVE", True):
        sql = rewrite_domain_sql(
            "SELECT * FROM _help_documents WHERE slug=%s"
        )
    assert "`PlatformData`.`_help_documents`" in sql


def test_already_target_qualified_table_is_not_rewritten_twice():
    with patch.object(settings, "PLATFORM_DOMAIN_ACTIVE", True):
        sql = rewrite_domain_sql("SELECT * FROM `PlatformData`.`_users`")
    assert sql == "SELECT * FROM `PlatformData`.`_users`"


def test_migration_whitelist_covers_required_domains_and_rejects_identifiers():
    assert {"platform", "visit", "dispatch", "work_logs", "registry_addresses"}.issubset(DOMAIN_TABLES)
    assert quote_identifier("_users") == "`_users`"
    try:
        quote_identifier("_users;DROP")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe identifier was accepted")


def test_quote_identifier_accepts_unicode_business_columns():
    assert quote_identifier("业务日期") == "`业务日期`"
