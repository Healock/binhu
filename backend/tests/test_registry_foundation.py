from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import settings
from routers import registry as registry_router
from routers.registry_extended import router as registry_extended_router
from routers.registry import PropertySearch, _property_search_result
from routers.registry_extended import _household_import_community
from services.permissions import (
    ALL_PERMISSIONS,
    DEFAULT_PERMISSION_GROUPS,
    REGISTRY_PROPERTY_MANAGE,
    REGISTRY_PROPERTY_VIEW,
    REGISTRY_WATCH_MANAGE,
    REGISTRY_WATCH_VIEW,
    WORKFLOW_TICKET_CREATE,
)
from services.registry_security import hmac_digest, normalize_identity, normalize_phone
from services.registry_import import (
    ISSUE_HOUSEHOLD_DUPLICATE,
    ISSUE_HOUSEHOLD_MISSING_TYPE,
    classify_household_rows,
    classify_certificate_rows,
    household_community_candidates,
    issue_problem_details,
    normalize_address,
    normalize_community,
)


def test_registry_normalization_and_hmac_are_deterministic():
    assert normalize_identity(" 320000 19990101999x ") == "32000019990101999X"
    assert normalize_phone("138-0000-0000") == "13800000000"
    first, version = hmac_digest(" 320000 19990101999x ", kind="identity")
    second, second_version = hmac_digest("32000019990101999X", kind="identity")
    assert first and first == second
    assert version == second_version == 1
    assert "32000019990101999" not in first


def test_empty_sensitive_values_do_not_create_digest():
    assert hmac_digest("", kind="identity") == (None, 1)
    assert hmac_digest("  ", kind="phone") == (None, 1)


def test_household_import_keeps_other_housing_types_as_normal_data():
    result = classify_household_rows([
        {"source_row": 1, "community": "芦荡社区", "address": "A 1", "housing_type": "借住"},
        {"source_row": 2, "community": "长板社区", "address": "B 2", "housing_type": "其他"},
        {"source_row": 3, "community": "长板社区", "address": "C 3", "housing_type": "其它"},
    ])
    assert result["issue_count"] == 0
    assert result["normal_count"] == 3
    assert result["other_type_count"] == 3
    assert normalize_community("芦荡社区") == "芦荡社区"


def test_household_community_candidates_keep_exact_alias_first():
    assert household_community_candidates(" 芦荡社区 ") == ["芦荡社区", "芦荡"]
    assert household_community_candidates("顾家荡社区居委会") == [
        "顾家荡社区居委会",
        "顾家荡社区",
        "顾家荡",
    ]
    assert household_community_candidates("南厍村") == ["南厍村", "南厍"]


class _CommunityCursor:
    def __init__(self, rows):
        self.rows = rows
        self.requested = []
        self.current = None

    async def execute(self, _sql, params):
        self.current = params[0]
        self.requested.append(self.current)

    async def fetchone(self):
        return self.rows.get(self.current)


@pytest.mark.asyncio
async def test_household_import_community_prefers_alias_then_suffix_fallback():
    alias_cursor = _CommunityCursor({"芦荡社区": (9, "长板社区", 1)})
    assert await _household_import_community(alias_cursor, "芦荡社区") == (
        9,
        "长板社区",
    )
    assert alias_cursor.requested == ["芦荡社区"]

    suffix_cursor = _CommunityCursor({"顾家荡社区": (3, "顾家荡社区", 1)})
    assert await _household_import_community(
        suffix_cursor,
        "顾家荡社区居委会",
    ) == (3, "顾家荡社区")
    assert suffix_cursor.requested == ["顾家荡社区居委会", "顾家荡社区"]


@pytest.mark.asyncio
async def test_household_import_community_does_not_bypass_disabled_exact_match():
    cursor = _CommunityCursor({"冬梅社区": (1, "冬梅社区", 0), "冬梅": (2, "冬梅", 1)})
    with pytest.raises(HTTPException) as error:
        await _household_import_community(cursor, "冬梅社区")
    assert error.value.status_code == 409
    assert cursor.requested == ["冬梅社区"]


def test_household_import_separates_duplicate_and_missing_type_issues():
    result = classify_household_rows([
        {"source_row": 10, "community": "长板社区", "address": "同一地址", "housing_type": "个人出租"},
        {"source_row": 11, "community": "长板社区", "address": "同一地址", "housing_type": "单位出租"},
        {"source_row": 12, "community": "长板社区", "address": "未标注", "housing_type": ""},
    ])
    issue_types = [item["issue_type"] for item in result["issues"]]
    assert issue_types.count(ISSUE_HOUSEHOLD_DUPLICATE) == 2
    assert issue_types.count(ISSUE_HOUSEHOLD_MISSING_TYPE) == 1
    assert result["normal_count"] == 0


def test_household_import_normalizes_full_width_and_cosmetic_address_separators():
    assert normalize_address("松陵镇南厍村22号") == normalize_address(" 松陵镇南厍村２－２号 ")
    assert normalize_address("长板社区（东区）1—2号") == normalize_address("长板社区东区12号")
    result = classify_household_rows([
        {"source_row": 20, "community": "长板社区", "address": "松陵镇南厍村22号", "housing_type": "个人出租"},
        {"source_row": 21, "community": "长板社区", "address": "松陵镇南厍村2-2号", "housing_type": "个人出租"},
    ])
    assert result["duplicate_groups"] == 1
    assert result["issue_count"] == 2


def test_household_import_does_not_merge_distinct_address_digits():
    result = classify_household_rows([
        {"source_row": 30, "community": "长板社区", "address": "松陵镇南厍村22号", "housing_type": "个人出租"},
        {"source_row": 31, "community": "长板社区", "address": "松陵镇南厍村23号", "housing_type": "个人出租"},
    ])
    assert result["duplicate_groups"] == 0
    assert result["normal_count"] == 2


def test_certificate_import_keeps_physical_rows_and_flags_content_conflicts():
    result = classify_certificate_rows([
        {"source_row": 1, "dz": "长板社区1号", "sssq": "长板社区", "czrxm": "甲"},
        {"source_row": 2, "dz": "长板社区1-号", "sssq": "长板社区", "czrxm": "乙"},
        {"source_row": 3, "dz": "长板社区2号", "sssq": "长板社区", "czrxm": "丙"},
    ])
    assert result["duplicate_groups"] == 1
    assert result["conflict_groups"] == 1
    assert result["issue_count"] == 4
    assert result["problem_row_count"] == 2
    assert result["normal_count"] == 1


def test_import_issue_evidence_names_field_and_current_bad_value():
    assert issue_problem_details(
        ISSUE_HOUSEHOLD_MISSING_TYPE,
        {"address": "长板社区1号", "housing_type": ""},
    ) == [{"field": "住房类型", "value": "（空白）"}]

    group = [
        {"address": "长板社区2号", "czrxm": "甲", "isSign": "已签署"},
        {"address": "长板社区2号", "czrxm": "乙", "isSign": "未签署"},
    ]
    details = issue_problem_details(
        "certificate_content_conflict",
        group[0],
        group_payloads=group,
    )
    assert {item["field"]: item["value"] for item in details} == {
        "房东姓名": "甲",
        "签署状态": "已签署",
    }


class _PropertySearchCursor:
    def __init__(self):
        self.calls = []
        self.mode = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        self.mode = "count" if sql.startswith("SELECT COUNT") else "rows"

    async def fetchone(self):
        return (12,)

    async def fetchall(self):
        return []


class _PropertySearchConnection:
    def __init__(self):
        self.search_cursor = _PropertySearchCursor()

    def cursor(self):
        return self.search_cursor


@pytest.mark.asyncio
async def test_property_search_combines_scope_keyword_community_and_housing_filters(monkeypatch):
    async def allowed_ids(_user, _permission):
        return [3, 4]

    monkeypatch.setattr(registry_router, "_allowed_community_ids", allowed_ids)
    conn = _PropertySearchConnection()
    result = await _property_search_result(
        PropertySearch(
            keyword="南厍",
            community_id=3,
            housing_category="rental",
            status="active",
            page=2,
            page_size=20,
        ),
        {"id": 1},
        conn,
    )

    count_sql, count_params = conn.search_cursor.calls[0]
    row_sql, row_params = conn.search_cursor.calls[1]
    assert "community_id IN" in count_sql
    assert "community_id=%s" in count_sql
    assert "housing_type IN" in count_sql
    assert "registry_address_aliases" in count_sql
    assert count_params.count("%南厍%") == 9
    assert row_params[-2:] == (20, 20)
    assert result == {"total": 12, "page": 2, "page_size": 20, "data": []}


@pytest.mark.asyncio
async def test_property_search_separates_not_required_from_pending_source_issues(monkeypatch):
    async def allowed_ids(_user, _permission):
        return None

    monkeypatch.setattr(registry_router, "_allowed_community_ids", allowed_ids)
    conn = _PropertySearchConnection()
    await _property_search_result(
        PropertySearch(certificate_status="not_required"),
        {"id": 1},
        conn,
    )

    count_sql, count_params = conn.search_cursor.calls[0]
    assert "certificate_issues.issue_count" in count_sql
    assert "certificate_source_state.source_ready" in count_sql
    assert "certificate_count,0)=0" in count_sql
    assert "issue_count,0)=0" in count_sql
    assert count_params == ("active", "个人出租", "单位出租")


def test_new_permissions_are_catalogued_and_defaulted():
    assert {REGISTRY_PROPERTY_VIEW, REGISTRY_PROPERTY_MANAGE, REGISTRY_WATCH_MANAGE,
            WORKFLOW_TICKET_CREATE}.issubset(ALL_PERMISSIONS)
    assert REGISTRY_PROPERTY_VIEW in DEFAULT_PERMISSION_GROUPS["internal_business"]["permissions"]
    assert WORKFLOW_TICKET_CREATE in DEFAULT_PERMISSION_GROUPS["flow_post"]["permissions"]
    assert settings.registry_hmac_key


def test_certificate_image_route_requires_import_permission():
    route = next(
        item for item in registry_extended_router.routes
        if item.path == "/api/registry/properties/{property_id}/certificates/{certificate_id}/image"
    )
    dependency_names = {
        dependency.call.__name__
        for dependency in route.dependant.dependencies
    }
    assert "require_registry_import_manage" in dependency_names


def test_property_detail_keeps_basic_view_permission():
    route = next(
        item for item in registry_extended_router.routes
        if item.path == "/api/registry/properties/{property_id}"
        and "GET" in item.methods
    )
    dependency_names = {
        dependency.call.__name__
        for dependency in route.dependant.dependencies
    }
    assert "require_registry_property_view" in dependency_names


def test_property_visit_history_keeps_basic_view_permission():
    route = next(
        item for item in registry_extended_router.routes
        if item.path == "/api/registry/properties/{property_id}/visits"
        and "GET" in item.methods
    )
    dependency_names = {
        dependency.call.__name__
        for dependency in route.dependant.dependencies
    }
    assert "require_registry_property_view" in dependency_names


class _RegistryTagCursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))

    async def fetchall(self):
        return self.rows


@pytest.mark.asyncio
async def test_registry_person_tags_use_explicit_link_with_legacy_hmac_fallback():
    cursor = _RegistryTagCursor([
        (
            7, 21, 3, "self_owned", "自购自住", "#16a34a", "notice",
            datetime(2026, 8, 1, 0, 0), None, None, "active", "资产导入", "registry", "batch:1",
        ),
    ])

    result = await registry_router._load_registry_person_categories(cursor, [7])

    sql, params = cursor.calls[0]
    assert "watch_person.registry_person_id=registry_person.id" in sql
    assert "watch_person.identity_hmac=registry_person.identity_hmac" in sql
    assert "assignment.valid_from<=UTC_TIMESTAMP()" in sql
    assert params == (7,)
    assert result[7][0]["category_name"] == "自购自住"
    assert registry_router._registry_person_category_payload(result[7][0]) == {
        "assignment_id": 21,
        "id": 3,
        "code": "self_owned",
        "name": "自购自住",
        "color": "#16a34a",
        "alert_level": "notice",
    }


def test_registry_person_tag_mutations_keep_watch_manage_permission():
    for path in {
        "/api/registry/people/{person_id}/tags",
        "/api/registry/people/{person_id}/tags/{assignment_id}/release",
    }:
        route = next(
            item for item in registry_extended_router.routes
            if item.path == path and "POST" in item.methods
        )
        dependency_names = {
            dependency.call.__name__
            for dependency in route.dependant.dependencies
        }
        assert "require_registry_watch_manage" in dependency_names


def test_registry_person_link_column_only_belongs_to_legacy_watch_people():
    source = (Path(__file__).resolve().parents[1] / "services" / "domain_schema.py").read_text(encoding="utf-8")
    housing = source.split("CREATE TABLE IF NOT EXISTS registry_housing_people", 1)[1].split(") ENGINE=", 1)[0]
    watch = source.split("CREATE TABLE IF NOT EXISTS watch_people", 1)[1].split(") ENGINE=", 1)[0]
    assert "registry_person_id" not in housing
    assert "UNIQUE KEY uk_registry_housing_identity (identity_hmac)," in housing
    assert "registry_person_id BIGINT DEFAULT NULL" in watch


def test_registry_tag_permissions_remain_separate_from_property_permissions():
    assert REGISTRY_WATCH_VIEW in ALL_PERMISSIONS
    assert REGISTRY_WATCH_MANAGE in ALL_PERMISSIONS
    assert REGISTRY_WATCH_MANAGE != REGISTRY_PROPERTY_MANAGE
