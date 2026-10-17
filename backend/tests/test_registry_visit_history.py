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
    def __init__(self, *, aliases=(), versions=(), visits=(), count=0, history=(), peers=(), communities=None, community_aliases=(), snapshots=(), identity_mappings=(), identity_properties=()):
        self.communities = communities if communities is not None else [(8, "测试社区"), (8, "长板社区"), (8, "合成社区"), (8, "测试甲社区"), (9, "测试乙社区")]
        self.community_aliases = list(community_aliases)
        self.snapshots = list(snapshots)
        self.aliases = list(aliases)
        self.versions = list(versions)
        self.visits = list(visits)
        self.count = count
        self.history = list(history)
        self.peers = list(peers)
        self.identity_mappings = list(identity_mappings)
        self.identity_properties = list(identity_properties)
        self.mode = ""
        self.calls = []

    async def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if "old_property_id,canonical_property_id,community_id,version" in sql:
            self.mode = "identity_mappings"
        elif "SELECT id,community_id FROM" in sql and "registry_properties" in sql:
            self.mode = "identity_properties"
        elif "._communities" in sql:
            self.mode = "communities"
        elif "._community_aliases" in sql:
            self.mode = "community_aliases"
        elif "SELECT DISTINCT community_id,community_name_snapshot" in sql:
            self.mode = "snapshots"
        elif "FROM registry_properties p" in sql:
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
            "communities": self.communities,
            "community_aliases": self.community_aliases,
            "snapshots": self.snapshots,
            "aliases": self.aliases,
            "versions": self.versions,
            "visits": self.visits,
            "history": self.history,
            "peers": self.peers,
            "identity_mappings": self.identity_mappings,
            "identity_properties": self.identity_properties,
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
            {"id": 1, "community_id": 8, "community_name": "测试社区", "natural_address": "合成新路1号"},
            {"id": 2, "community_id": 8, "community_name": "测试社区", "natural_address": "合成路2号"},
        ], visit_status="never")
        self.assertEqual({2}, result)
        self.assertNotIn("业务日期", cursor.calls[-1][0])

    async def test_shared_address_with_visits_is_not_claimed_never_visited(self):
        cursor = _Cursor(visits=[("测试社区", visit_address_key("合成路1号"))])
        result = await filter_property_ids_by_visit(cursor, [
            {"id": 1, "community_id": 8, "community_name": "测试社区", "natural_address": "合成路1号"},
            {"id": 2, "community_id": 8, "community_name": "测试社区", "natural_address": "合成路1号"},
        ], visit_status="never")
        self.assertEqual(set(), result)

    async def test_never_visited_does_not_cross_communities(self):
        cursor = _Cursor(visits=[("测试乙社区", visit_address_key("合成路1号"))])
        result = await filter_property_ids_by_visit(cursor, [
            {"id": 1, "community_id": 8, "community_name": "测试甲社区", "natural_address": "合成路1号"},
        ], visit_status="never")
        self.assertEqual({1}, result)

    async def test_never_visited_without_records_returns_all_candidates(self):
        result = await filter_property_ids_by_visit(_Cursor(), [
            {"id": 1, "community_id": 8, "community_name": "测试社区", "natural_address": "合成路1号"},
            {"id": 2, "community_id": 8, "community_name": "测试社区", "natural_address": "合成路2号"},
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
                {"id": 1, "community_id": 8, "community_name": "长板社区", "natural_address": "长板社区1号", "normalized_address": "长板社区1号"},
                {"id": 2, "community_id": 8, "community_name": "长板社区", "natural_address": "长板社区2号", "normalized_address": "长板社区2号"},
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
            "community_id": 8, "community_name": "长板社区",
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
        self.assertIn("`社区` IN (", visit_sql)

    async def test_property_visit_summary_does_not_attach_ambiguous_same_address(self):
        cursor = _Cursor()
        summaries = await load_property_visit_summaries(cursor, [
            {"id": 1, "community_id": 8, "community_name": "长板社区", "natural_address": "同址1号", "normalized_address": "同址1号"},
            {"id": 2, "community_id": 8, "community_name": "长板社区", "natural_address": "同址1号", "normalized_address": "同址1号"},
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
        current = {"id": 1, "community_id": 8, "community_name": "合成社区", "natural_address": "合成路8号", "normalized_address": "合成路8号"}
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
        prop = {"id": 1, "community_id": 8, "community_name": "合成社区", "natural_address": "合成路8号"}
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
            {"community_id": 8, "community_name": "长板社区", "natural_address": "长板社区1号", "normalized_address": "长板社区1号"},
            [],
            page=2,
            page_size=1,
        )
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["page"], 2)
        self.assertEqual(result["data"][0]["star_rating"], "四星出租房")
        self.assertEqual(result["data"][0]["business_date"], "2026-08-25")
        self.assertEqual(cursor.calls[-1][1][-2:], (1, 1))


    async def test_community_rename_uses_id_for_peers_and_all_safe_names_for_visits(self):
        key = visit_address_key("合成旧路8号")
        peers = [
            (1, 8, "", "合成路8号", "", "", "合成路8号", "NEW", "2026-01-01", "个人出租", "", "未注销", "新合成社区"),
            (2, 8, "", "合成路8号", "", "", "合成路8号", "OLD", "2020-01-01", "个人出租", "", "已注销", "旧合成社区"),
        ]
        prop = {"id": 1, "community_id": 8, "community_name": "过期快照", "natural_address": "合成路8号"}
        options = dict(peers=peers, communities=[(8, "新合成社区")],
                       community_aliases=[(8, "旧合成社区")], snapshots=[(8, "过期快照")],
                       aliases=[(1, "合成旧路8号")])
        cursor = _Cursor(**options, visits=[(10, "旧合成社区", key, date(2026, 10, 10), None, "五星出租房", None)])
        summary = await load_property_visit_summaries(cursor, [prop])
        self.assertEqual(summary[1]["visit_count"], 1)
        peer_sql, params = next((s, p) for s, p in cursor.calls if "FROM registry_properties p" in s)
        self.assertIn("p.community_id IN", peer_sql)
        self.assertNotIn("p.community_name_snapshot IN", peer_sql)
        self.assertEqual(params[0], 8)
        self.assertTrue({"新合成社区", "旧合成社区", "过期快照"}.issubset(set(cursor.calls[-1][1])))
        filtered = await filter_property_ids_by_visit(_Cursor(**options, visits=[("旧合成社区", key)]), [prop])
        self.assertEqual(filtered, {1})
        history_cursor = _Cursor(**options, count=1)
        history = await load_property_visit_history(history_cursor, prop, ["合成旧路8号"], page=1, page_size=20)
        self.assertEqual(history["total"], 1)
        self.assertIn("旧合成社区", history_cursor.calls[-1][1])


    async def test_historical_name_conflict_fails_closed_across_community_ids(self):
        prop = {"id": 1, "community_id": 8, "community_name": "共用旧称", "natural_address": "合成路8号"}
        key = visit_address_key("合成路8号")
        options = dict(communities=[(8, "甲合成社区"), (9, "乙合成社区")],
                       community_aliases=[(8, "共用旧称")], snapshots=[(9, "共用旧称")])
        summary = await load_property_visit_summaries(_Cursor(**options, visits=[(10, "共用旧称", key, date(2026, 10, 10), None, None, None)]), [prop])
        self.assertEqual(summary[1]["visit_count"], 0)
        self.assertEqual(summary[1]["visit_association_status"], "community_unresolved")
        filtered = await filter_property_ids_by_visit(_Cursor(**options, visits=[("共用旧称", key)]), [prop])
        self.assertEqual(filtered, set())
        never = await filter_property_ids_by_visit(_Cursor(**options), [prop], visit_status="never")
        self.assertEqual(never, set())
        history = await load_property_visit_history(_Cursor(**options), prop, [], page=1, page_size=20)
        self.assertEqual(history["visit_association_status"], "community_unresolved")


    async def test_missing_community_identity_is_not_inferred_from_name(self):
        prop = {"id": 1, "community_name": "测试社区", "natural_address": "合成路8号"}
        cursor = _Cursor()
        summary = await load_property_visit_summaries(cursor, [prop])
        self.assertEqual(summary[1]["visit_association_status"], "community_unresolved")
        self.assertEqual(await filter_property_ids_by_visit(_Cursor(), [prop], visit_status="never"), set())
        history = await load_property_visit_history(_Cursor(), prop, [], page=1, page_size=20)
        self.assertEqual(history["visit_association_status"], "community_unresolved")
        self.assertFalse(any("t_visit_details" in sql for sql, _ in cursor.calls))

    async def test_organization_queries_follow_configured_domain_switch(self):
        from unittest.mock import patch
        from services.registry_visit_history import settings
        for active, schema in ((True, "Staging_PlatformData"), (False, "Staging_OnlineData")):
            cursor = _Cursor()
            with patch.object(settings, "PLATFORM_DOMAIN_ACTIVE", active), \
                 patch.object(settings, "MYSQL_PLATFORM_DB", "Staging_PlatformData"), \
                 patch.object(settings, "MYSQL_ONLINE_DATA_DB", "Staging_OnlineData"):
                await load_property_visit_summaries(cursor, [{"id": 1, "community_id": 8,
                                                           "natural_address": "合成路8号"}])
            self.assertTrue(any(f"`{schema}`._communities" in sql for sql, _ in cursor.calls))
            self.assertTrue(any(f"`{schema}`._community_aliases" in sql for sql, _ in cursor.calls))

    async def test_detail_and_history_callers_pass_stable_community_id(self):
        from unittest.mock import AsyncMock, patch
        from routers import registry_extended as router
        row = (1, "", 8, "合成旧社区", "合成路8号", "", "", "个人出租", "", "NEW",
               None, "household", "", "合成路8号", "active", 1, None, None, "未注销")
        cursor = AsyncMock()
        cursor.fetchall.return_value = []
        cursor.fetchone.side_effect = [row, None, (0, 0)]
        conn = AsyncMock()
        conn.cursor = lambda: conn
        conn.__aenter__.return_value = cursor
        summaries = AsyncMock(return_value={1: {"visit_count": 0}})
        history = AsyncMock(return_value={"data": [], "total": 0})
        with patch.object(router, "_property_scope", AsyncMock()), \
             patch.object(router, "load_property_visit_summaries", summaries), \
             patch.object(router, "load_property_visit_history", history), \
             patch.object(router, "load_property_address_variants", AsyncMock(return_value={})):
            await router.get_property_detail(1, user={"permissions": []}, conn=conn)
            self.assertEqual(summaries.call_args.args[1][0]["community_id"], 8)
            cursor.fetchone.side_effect = [(1, "合成旧社区", "合成路8号", "合成路8号", 8)]
            await router.get_property_visit_history(1, page=1, page_size=20, user={}, conn=conn)
            self.assertEqual(history.call_args.args[1]["community_id"], 8)
