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
