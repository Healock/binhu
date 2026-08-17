import json
import unittest

from services.qmf_community import (
    DEFAULT_QMF_COMMUNITY_CODES,
    QMF_COMMUNITY_CODE_SEED_MARKER,
    resolve_qmf_community,
    seed_default_qmf_community_codes,
)


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
    def test_default_community_codes_match_verified_twelve_row_source(self):
        self.assertEqual(len(DEFAULT_QMF_COMMUNITY_CODES), 12)
        self.assertEqual(DEFAULT_QMF_COMMUNITY_CODES["三船港"], "320584037C")
        self.assertEqual(DEFAULT_QMF_COMMUNITY_CODES["祥泰"], "320584021E")
        self.assertEqual(DEFAULT_QMF_COMMUNITY_CODES["龙河"], "320584037A")
        self.assertTrue(all(
            len(code) == 10 and code.isalnum() and code == code.upper()
            for code in DEFAULT_QMF_COMMUNITY_CODES.values()
        ))

    async def test_default_code_seed_runs_once_and_only_targets_blank_values(self):
        class SeedCursor:
            def __init__(self):
                self.commands = []
                self.marker_exists = False
                self.rows = []
                self.update_calls = []

            async def execute(self, sql, params=None):
                normalized = " ".join(sql.split())
                self.commands.append((normalized, params))
                if normalized == "START TRANSACTION":
                    return
                if normalized.startswith("SELECT config_value FROM _system_config"):
                    self.rows = [("0.21.9",)] if self.marker_exists else []
                    return
                if normalized.startswith("UPDATE _communities"):
                    self.update_calls.append((normalized, params))
                    return
                if normalized.startswith("INSERT INTO _system_config"):
                    self.assert_marker_params(params)
                    self.marker_exists = True
                    return
                if normalized in {"COMMIT", "ROLLBACK"}:
                    return
                raise AssertionError(f"unexpected SQL: {normalized}")

            async def fetchone(self):
                return self.rows[0] if self.rows else None

            @staticmethod
            def assert_marker_params(params):
                if params != (QMF_COMMUNITY_CODE_SEED_MARKER, "0.21.9"):
                    raise AssertionError(f"unexpected marker params: {params}")

        cursor = SeedCursor()
        self.assertTrue(await seed_default_qmf_community_codes(cursor))
        self.assertEqual(len(cursor.update_calls), 12)
        for sql, (code, name) in cursor.update_calls:
            self.assertIn("qmf_community_code IS NULL", sql)
            self.assertIn("qmf_community_code=''", sql)
            self.assertEqual(DEFAULT_QMF_COMMUNITY_CODES[name], code)
        self.assertTrue(cursor.marker_exists)

        update_count = len(cursor.update_calls)
        self.assertFalse(await seed_default_qmf_community_codes(cursor))
        self.assertEqual(len(cursor.update_calls), update_count)
        self.assertEqual(cursor.commands[-1][0], "COMMIT")

    async def test_source_community_alias_resolves_configured_code(self):
        cursor = FakeCursor(
            communities=[(1, "冬梅社区", "320584037C")],
            aliases=[(1, "冬梅")],
        )
        resolved = await resolve_qmf_community(
            cursor, source_community="冬梅", address="虚构地址"
        )
        self.assertEqual(resolved.name, "冬梅社区")
        self.assertEqual(resolved.qmf_community_code, "320584037C")

    async def test_lowercase_qmf_code_is_normalized_to_uppercase(self):
        cursor = FakeCursor(communities=[(1, "冬梅社区", "320584037c")])
        resolved = await resolve_qmf_community(
            cursor, source_community="冬梅社区", address=""
        )
        self.assertEqual(resolved.qmf_community_code, "320584037C")

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
