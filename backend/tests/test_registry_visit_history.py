from __future__ import annotations

from datetime import date, datetime
import os
import unittest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.registry_visit_history import (
    filter_property_ids_by_visit,
    load_property_visit_history,
    load_property_visit_summaries,
    property_visit_keys,
    visit_address_key,
)


class _Cursor:
    def __init__(self, *, aliases=(), versions=(), visits=(), count=0, history=(), peers=()):
        self.aliases = list(aliases)
        self.versions = list(versions)
        self.visits = list(visits)
        self.count = count
        self.history = list(history)
        self.peers = list(peers)
        self.mode = ""
        self.calls = []

    async def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if "FROM registry_properties p" in sql:
            self.mode = "peers"
        elif "FROM registry_address_aliases" in sql:
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
            "peers": self.peers,
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
    async def test_never_visited_checks_all_history_including_old_addresses(self):
        cursor = _Cursor(
            versions=[(1, "合成旧路1号", "合成旧路1号")],
            visits=[("测试社区", visit_address_key("合成旧路1号"))],
        )
        result = await filter_property_ids_by_visit(cursor, [
            {"id": 1, "community_name": "测试社区", "natural_address": "合成新路1号"},
            {"id": 2, "community_name": "测试社区", "natural_address": "合成路2号"},
        ], visit_status="never")
        self.assertEqual({2}, result)
        self.assertNotIn("业务日期", cursor.calls[-1][0])

    async def test_shared_address_with_visits_is_not_claimed_never_visited(self):
        cursor = _Cursor(visits=[("测试社区", visit_address_key("合成路1号"))])
        result = await filter_property_ids_by_visit(cursor, [
            {"id": 1, "community_name": "测试社区", "natural_address": "合成路1号"},
            {"id": 2, "community_name": "测试社区", "natural_address": "合成路1号"},
        ], visit_status="never")
        self.assertEqual(set(), result)

    async def test_never_visited_does_not_cross_communities(self):
        cursor = _Cursor(visits=[("测试乙社区", visit_address_key("合成路1号"))])
        result = await filter_property_ids_by_visit(cursor, [
            {"id": 1, "community_name": "测试甲社区", "natural_address": "合成路1号"},
        ], visit_status="never")
        self.assertEqual({1}, result)

    async def test_never_visited_without_records_returns_all_candidates(self):
        result = await filter_property_ids_by_visit(_Cursor(), [
            {"id": 1, "community_name": "测试社区", "natural_address": "合成路1号"},
            {"id": 2, "community_name": "测试社区", "natural_address": "合成路2号"},
        ], visit_status="never")
        self.assertEqual({1, 2}, result)

    async def test_filter_property_ids_by_visit_applies_date_and_star_before_pagination(self):
        cursor = _Cursor(
            aliases=[],
            visits=[
                ("长板社区", visit_address_key("长板社区1号")),
            ],
        )
        matched = await filter_property_ids_by_visit(
            cursor,
            [
                {"id": 1, "community_name": "长板社区", "natural_address": "长板社区1号", "normalized_address": "长板社区1号"},
                {"id": 2, "community_name": "长板社区", "natural_address": "长板社区2号", "normalized_address": "长板社区2号"},
            ],
            visit_start_date=date(2026, 8, 20),
            visit_end_date=date(2026, 8, 31),
            star_ratings=["三星出租房"],
        )
        self.assertEqual(matched, {1})
        sql, params = cursor.calls[-1]
        self.assertIn("`业务日期` >= %s", sql)
        self.assertIn("`业务日期` <= %s", sql)
        self.assertIn("`星级` IN (%s)", sql)
        self.assertEqual(params[-3:], (date(2026, 8, 20), date(2026, 8, 31), "三星出租房"))

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
        self.assertEqual(summaries[1]["visit_association_status"], "ambiguous")
        self.assertFalse(any("t_visit_details" in sql for sql, _ in cursor.calls))

    async def test_reregistered_house_has_same_summary_on_any_page(self):
        def peer(identifier, number, when, status):
            return (identifier, 8, "", "合成路8号", "", "", "合成路8号", number,
                    when, "个人出租", "", status, "合成社区")
        peers = [peer(1, "NEW", "2026-01-01", "未注销"), peer(2, "OLD", "2020-01-01", "已注销")]
        current = {"id": 1, "community_name": "合成社区", "natural_address": "合成路8号", "normalized_address": "合成路8号"}
        old = {**current, "id": 2}
        visits = [(10, "合成社区", visit_address_key("合成路8号"), date(2026, 10, 10), None, "五星出租房", None)]
        alone = await load_property_visit_summaries(_Cursor(peers=peers, visits=visits), [current])
        together = await load_property_visit_summaries(_Cursor(peers=peers, visits=visits), [current, old])
        self.assertEqual(alone[1], together[1])
        self.assertEqual(alone[1]["visit_count"], 1)
        self.assertEqual(alone[1]["latest_visit_date"], "2026-10-10")
        self.assertEqual(together[2]["visit_association_status"], "historical_registration")
        filtered = await filter_property_ids_by_visit(_Cursor(peers=peers, visits=[("合成社区", visit_address_key("合成路8号"))]), [current])
        self.assertEqual(filtered, {1})

    async def test_filtered_out_conflicting_archive_still_blocks_association(self):
        peers = [(i, 8, "", "合成路8号", "", "", "合成路8号", str(i), "2026-01-01",
                  "个人出租", "", "未注销", "合成社区") for i in (1, 2)]
        prop = {"id": 1, "community_name": "合成社区", "natural_address": "合成路8号"}
        result = await load_property_visit_summaries(_Cursor(peers=peers), [prop])
        self.assertEqual(result[1]["visit_association_status"], "ambiguous")
        history = await load_property_visit_history(_Cursor(peers=peers), prop, [], page=1, page_size=50)
        self.assertEqual(history["visit_association_status"], "ambiguous")

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
