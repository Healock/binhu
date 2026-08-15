from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from config import settings
from routers.registry_extended import _household_import_community
from services.permissions import (
    ALL_PERMISSIONS,
    DEFAULT_PERMISSION_GROUPS,
    REGISTRY_PROPERTY_MANAGE,
    REGISTRY_PROPERTY_VIEW,
    REGISTRY_WATCH_MANAGE,
    WORKFLOW_TICKET_CREATE,
)
from services.registry_security import hmac_digest, normalize_identity, normalize_phone
from services.registry_import import (
    ISSUE_HOUSEHOLD_DUPLICATE,
    ISSUE_HOUSEHOLD_MISSING_TYPE,
    classify_household_rows,
    classify_certificate_rows,
    household_community_candidates,
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


def test_new_permissions_are_catalogued_and_defaulted():
    assert {REGISTRY_PROPERTY_VIEW, REGISTRY_PROPERTY_MANAGE, REGISTRY_WATCH_MANAGE,
            WORKFLOW_TICKET_CREATE}.issubset(ALL_PERMISSIONS)
    assert REGISTRY_PROPERTY_VIEW in DEFAULT_PERMISSION_GROUPS["internal_business"]["permissions"]
    assert WORKFLOW_TICKET_CREATE in DEFAULT_PERMISSION_GROUPS["flow_post"]["permissions"]
    assert settings.registry_hmac_key
