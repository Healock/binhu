import json
import unittest

from services.qmf_community import resolve_qmf_community


class FakeCursor:
    def __init__(self, communities, aliases=(), entries=()):
        self.communities = communities
        self.aliases = aliases
        self.entries = entries
        self.rows = []

    async def execute(self, sql, _params=None):
        normalized = " ".join(str(sql).split())
        if "FROM _communities AS community" in normalized:
            self.rows = list(self.communities)
        elif "FROM _community_aliases" in normalized:
            self.rows = list(self.aliases)
        elif "FROM _police_address_entries AS entry" in normalized:
            self.rows = list(self.entries)
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")

    async def fetchall(self):
        return list(self.rows)


class QmfCommunityTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_community_alias_resolves_configured_code(self):
        cursor = FakeCursor(
            communities=[(1, "冬梅社区", "3205840001")],
            aliases=[(1, "冬梅")],
        )
        resolved = await resolve_qmf_community(
            cursor, source_community="冬梅", address="虚构地址"
        )
        self.assertEqual(resolved.name, "冬梅社区")
        self.assertEqual(resolved.qmf_community_code, "3205840001")

    async def test_address_entry_resolves_when_source_community_is_empty(self):
        cursor = FakeCursor(
            communities=[(1, "冬梅社区", "3205840001")],
            entries=[(
                1,
                "天铂商业广场",
                "虚构路1号",
                json.dumps(["天铂广场"], ensure_ascii=False),
            )],
        )
        resolved = await resolve_qmf_community(
            cursor,
            source_community="",
            address="虚构市天铂商业广场3幢702室",
        )
        self.assertEqual(resolved.id, 1)

    async def test_conflicting_source_and_address_are_rejected(self):
        cursor = FakeCursor(
            communities=[
                (1, "冬梅社区", "3205840001"),
                (2, "夏荷社区", "3205840002"),
            ],
            entries=[(2, "测试小区", "", "[]")],
        )
        with self.assertRaisesRegex(ValueError, "community_conflict"):
            await resolve_qmf_community(
                cursor,
                source_community="冬梅社区",
                address="测试小区1幢",
            )

    async def test_missing_qmf_code_stops_before_feedback(self):
        cursor = FakeCursor(communities=[(1, "冬梅社区", "")])
        with self.assertRaisesRegex(ValueError, "community_code_missing"):
            await resolve_qmf_community(
                cursor, source_community="冬梅社区", address=""
            )


if __name__ == "__main__":
    unittest.main()
