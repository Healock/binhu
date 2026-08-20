import os
import unittest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.dashboard_scope import (
    dashboard_communities,
    intersect_scopes,
    responsibility_label,
    role_responsibility_communities,
)
from routers.dashboard import _community_breakdown
from services.data_scope import filter_report_payload


class ScopeCursor:
    def __init__(self, rows_by_kind=None):
        self.rows_by_kind = rows_by_kind or {}
        self.rows = []
        self.executed_sql = []

    async def execute(self, sql, params=()):
        self.executed_sql.append(sql)
        if "_area_leader_links" in sql:
            self.rows = list(self.rows_by_kind.get("areas", []))
        elif "FROM _communities" in sql:
            accepted = set(params)
            self.rows = [
                row for row in self.rows_by_kind.get("communities", [])
                if row[0] in accepted
            ]
        else:
            self.rows = []

    async def fetchall(self):
        return list(self.rows)


def user(position="", *, communities=None, scope="all", member=True):
    return {
        "role": "member",
        "member": {"id": 9, "name": "测试人员", "position": position}
        if member else None,
        "community_names": communities or [],
        "data_scope": scope,
        "permission_scopes": {"online.summary.view": scope},
        "permission_groups": [],
    }


class DashboardScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_community_breakdown_keeps_zero_count_scoped_communities(self):
        report = {
            "community": {
                "data": [
                    {
                        "社区": "长板社区",
                        "数据总数": 10,
                        "已完成": 6,
                        "无法见底数": 2,
                    },
                ],
            },
        }
        result = _community_breakdown(
            report,
            ["长板社区", "冬梅社区"],
        )
        self.assertEqual({item["community"] for item in result}, {"长板社区", "冬梅社区"})
        winter = next(item for item in result if item["community"] == "冬梅社区")
        self.assertEqual(winter["total"], 0)
        self.assertEqual(winter["completed"], 0)

    async def test_role_responsibility_covers_station_area_and_active_community(self):
        cursor = ScopeCursor({
            "areas": [("长板社区",), ("龙河社区",)],
            "communities": [("长板社区",)],
        })
        self.assertIsNone(
            await role_responsibility_communities(cursor, user("基础管控"))
        )
        self.assertEqual(
            await role_responsibility_communities(cursor, user("片长")),
            ["长板社区", "龙河社区"],
        )
        self.assertEqual(
            await role_responsibility_communities(
                cursor,
                user("组员", communities=["长板社区", "停用社区"]),
            ),
            ["长板社区"],
        )
        community_sql = next(
            sql for sql in cursor.executed_sql
            if "LEFT JOIN _community_aliases" in sql
        )
        self.assertIn("ORDER BY community.name", community_sql)
        self.assertNotIn("ORDER BY community.id", community_sql)
        self.assertEqual(responsibility_label("组员", ["长板社区"]), "本人")
        self.assertEqual(responsibility_label("自购房", []), "本人")

    async def test_unlinked_admin_is_station_wide_and_other_account_has_no_area(self):
        admin = user(member=False)
        admin["role"] = "admin"
        self.assertIsNone(
            await role_responsibility_communities(ScopeCursor(), admin)
        )
        self.assertEqual(
            await role_responsibility_communities(
                ScopeCursor(), user("其他岗位")
            ),
            [],
        )

    async def test_permission_scope_can_only_narrow_role_responsibility(self):
        cursor = ScopeCursor({"communities": [("长板社区",), ("龙河社区",)]})
        scoped = user(
            "社区民警",
            communities=["长板社区", "龙河社区"],
            scope="own_department",
        )
        scoped["departments"] = [{
            "type": "community",
            "community_name": "长板社区",
        }]
        self.assertEqual(
            await dashboard_communities(
                cursor, scoped, "online.summary.view"
            ),
            ["长板社区"],
        )
        self.assertEqual(intersect_scopes(None, ["长板社区"]), ["长板社区"])
        self.assertEqual(
            responsibility_label("片长", ["长板社区"]),
            "负责片区：长板社区",
        )

    async def test_personal_report_scope_never_leaks_community_aggregate(self):
        payload = {
            "exists": True,
            "inspector": {"data": [
                {"社区": "长板社区", "姓名": "甲", "已完成": 3},
                {"社区": "长板社区", "姓名": "乙", "已完成": 8},
            ], "summary": {"已完成": 11}},
            "community": {"data": [
                {"社区": "长板社区", "已完成": 11},
            ], "summary": {"已完成": 11}},
            "data": [{"社区": "长板社区", "已完成": 11}],
        }
        scoped = filter_report_payload(
            payload,
            user("组员", communities=["长板社区"]),
            ["长板社区"],
            ["甲"],
        )
        self.assertEqual(scoped["inspector"]["data"], [
            {"社区": "长板社区", "姓名": "甲", "已完成": 3},
        ])
        self.assertEqual(scoped["community"]["data"], [])
        self.assertEqual(scoped["data"], [])


if __name__ == "__main__":
    unittest.main()
