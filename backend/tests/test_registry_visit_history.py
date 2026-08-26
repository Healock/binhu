from __future__ import annotations

from datetime import date, datetime
import os
import unittest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.registry_visit_history import (
    load_property_visit_history,
    load_property_visit_summaries,
    property_visit_keys,
    visit_address_key,
)


class _Cursor:
    def __init__(self, *, aliases=(), versions=(), visits=(), count=0, history=()):
        self.aliases = list(aliases)
        self.versions = list(versions)
        self.visits = list(visits)
        self.count = count
        self.history = list(history)
        self.mode = ""
        self.calls = []

    async def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if "FROM registry_address_aliases" in sql:
            self.mode = "aliases"
        elif "FROM registry_property_address_versions" in sql:
            self.mode = "versions"
        elif sql.startswith("SELECT COUNT"):
            self.mode = "count"
        elif "ORDER BY `业务日期` DESC" in sql:
            self.mode = "history"
        else:
            self.mode = "visits"

    async def fetchone(self):
        return (self.count,)

    async def fetchall(self):
        return {
            "aliases": self.aliases,
            "versions": self.versions,
            "visits": self.visits,
            "history": self.history,
        }.get(self.mode, [])


class RegistryVisitKeyTests(unittest.TestCase):
    def test_property_visit_keys_include_current_alias_and_old_address(self):
        keys = property_visit_keys(
            {
                "natural_address": "长板社区 1-2 号",
                "normalized_address": "长板社区12号",
            },
            ["旧称一号", "历史地址二号"],
        )
        self.assertIn(visit_address_key("长板社区 1-2 号"), keys)
        self.assertIn(visit_address_key("旧称一号"), keys)
        self.assertIn(visit_address_key("历史地址二号"), keys)
        self.assertNotIn("", keys)


class RegistryVisitHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_property_visit_summary_uses_alias_and_keeps_latest_dates(self):
        alias_key = visit_address_key("旧地址 8 号")
        cursor = _Cursor(
            aliases=[(7, "旧地址 8 号")],
            visits=[
                (10, "长板社区", alias_key, date(2026, 8, 20), datetime(2026, 8, 20, 2), None, None),
                (11, "长板社区", alias_key, date(2026, 8, 25), datetime(2026, 8, 25, 3), "三星出租房", datetime(2026, 8, 25, 4)),
            ],
        )
        summaries = await load_property_visit_summaries(cursor, [{
            "id": 7,
            "community_name": "长板社区",
            "natural_address": "新地址 8 号",
            "normalized_address": "新地址8号",
        }])
        self.assertEqual(summaries[7], {
            "visit_count": 2,
            "latest_visit_date": "2026-08-25",
            "latest_star_rating": "三星出租房",
            "latest_star_rating_at": "2026-08-25T04:00:00",
        })
        visit_sql = cursor.calls[-1][0]
        self.assertIn("`VisitData`.`t_visit_details`", visit_sql)
        self.assertIn("`社区`=%s", visit_sql)

    async def test_property_visit_summary_does_not_attach_ambiguous_same_address(self):
        cursor = _Cursor()
        summaries = await load_property_visit_summaries(cursor, [
            {"id": 1, "community_name": "长板社区", "natural_address": "同址1号", "normalized_address": "同址1号"},
            {"id": 2, "community_name": "长板社区", "natural_address": "同址1号", "normalized_address": "同址1号"},
        ])
        self.assertEqual(summaries[1]["visit_count"], 0)
        self.assertEqual(summaries[2]["visit_count"], 0)
        self.assertFalse(any("t_visit_details" in sql for sql, _ in cursor.calls))

    async def test_property_visit_history_is_paginated_and_returns_rating(self):
        cursor = _Cursor(
            count=3,
            history=[(
                21, "长板社区", "扫码", "长板社区1号", "网格员甲",
                datetime(2026, 8, 25, 1), date(2026, 8, 25), 2, 1, 0, 1,
                "四星出租房", 92.5, datetime(2026, 8, 25, 2), date(2026, 8, 25),
            )],
        )
        result = await load_property_visit_history(
            cursor,
            {"community_name": "长板社区", "natural_address": "长板社区1号", "normalized_address": "长板社区1号"},
            [],
            page=2,
            page_size=1,
        )
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["page"], 2)
        self.assertEqual(result["data"][0]["star_rating"], "四星出租房")
        self.assertEqual(result["data"][0]["business_date"], "2026-08-25")
        self.assertEqual(cursor.calls[-1][1][-2:], (1, 1))
