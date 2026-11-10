import os

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

import pytest

from routers.mobile_tasks import _address_matches_by_rows


class Cursor:
    def __init__(self):
        self.query = ""
        self.params = ()

    async def execute(self, query, params):
        self.query = " ".join(query.split())
        self.params = params

    async def fetchall(self):
        return [("row-1", 7, "测试小区", "suggested", 0.9, "rule", "", "[]", "v1")]


@pytest.mark.asyncio
async def test_address_match_query_uses_unambiguous_alias_and_keeps_result_mapping():
    cursor = Cursor()

    result = await _address_matches_by_rows(cursor, "全链条", ["row-1"])

    assert "AS address_match" in cursor.query
    assert "AS match " not in cursor.query
    assert "address_match.parser_type=%s" in cursor.query
    assert cursor.params == ("全链条", "row-1")
    assert result["row-1"] == {
        "small_community_id": 7,
        "small_community_name": "测试小区",
        "status": "suggested",
        "score": 0.9,
        "method": "rule",
        "reason": "",
        "candidates": [],
        "version": "v1",
    }
