import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from tools.migrate_personnel_accounts import (
    _build_preview,
    _read_overrides,
    _validate_expected_preview,
)


class PreviewCursor:
    def __init__(self, *, users=None, members=None, communities=None):
        self.users = users or []
        self.members = members or []
        self.communities = communities or []
        self.result = []
        self.calls = []

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if "FROM _users" in normalized:
            self.result = self.users
        elif "FROM _grid_members" in normalized:
            self.result = self.members
        elif "FROM _communities AS community" in normalized:
            self.result = self.communities
        elif "department_type='internal'" in normalized:
            self.result = [(30, "内勤")]
        elif "FROM _position_permission_group_links" in normalized:
            position = str((params or [""])[0])
            self.result = {
                "社区民警": [(40, "admin")],
                "所队领导": [(40, "admin")],
                "基础管控": [(30, "internal_business")],
            }.get(position, [])
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")

    async def fetchall(self):
        return list(self.result)

    async def fetchone(self):
        return self.result[0] if self.result else None


class PersonnelAccountMigrationTests(unittest.IsolatedAsyncioTestCase):
    def test_private_overrides_are_strictly_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            valid_path = Path(directory) / "valid.json"
            valid_path.write_text(
                json.dumps({"account-a": "基础管控"}),
                encoding="utf-8",
            )
            self.assertEqual(
                _read_overrides(str(valid_path)),
                {"account-a": "基础管控"},
            )

            invalid_path = Path(directory) / "invalid.json"
            invalid_path.write_text(
                json.dumps({"account-a": "组员"}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _read_overrides(str(invalid_path))

    async def test_preview_selects_unlinked_accounts_and_counts_multi_community(self):
        cursor = PreviewCursor(
            users=[
                (1, "already-linked", 90, "member"),
                (2, "officer-a", None, "admin"),
                (3, "leader-a", None, "admin"),
                (4, "basic-a", None, "admin"),
            ],
            members=[(90, "已有人员")],
            communities=[
                (11, "南厍", '["民警甲"]', 21),
                (12, "阅湖", '["民警甲"]', 22),
            ],
        )
        rows = [
            {"username": "already-linked", "name": "已有人员", "position": "流口岗"},
            {"username": "officer-a", "name": "民警甲", "position": "社区民警"},
            {"username": "leader-a", "name": "领导甲", "position": "所（队）领导"},
            {"username": "basic-a", "name": "内勤甲", "position": "内勤岗"},
        ]

        preview = await _build_preview(
            cursor,
            rows,
            {"basic-a": "基础管控"},
        )

        self.assertEqual(len(preview), 3)
        officer = next(item for item in preview if item["position"] == "社区民警")
        self.assertEqual(
            [item["community_name"] for item in officer["departments"]],
            ["南厍", "阅湖"],
        )
        self.assertTrue(all(call[0].startswith("SELECT") for call in cursor.calls))

    async def test_preview_rejects_member_owned_by_another_account(self):
        cursor = PreviewCursor(
            users=[
                (1, "owner", 90, "admin"),
                (2, "officer-a", None, "admin"),
            ],
            members=[(90, "民警甲")],
            communities=[(11, "南厍", '["民警甲"]', 21)],
        )
        rows = [{
            "username": "officer-a",
            "name": "民警甲",
            "position": "社区民警",
        }]

        with self.assertRaisesRegex(ValueError, "已经关联其他账号"):
            await _build_preview(cursor, rows, {})

    async def test_preview_rejects_override_outside_source_file(self):
        cursor = PreviewCursor(users=[])
        with self.assertRaisesRegex(ValueError, "不在名单中"):
            await _build_preview(
                cursor,
                [{"username": "known", "name": "人员甲", "position": "内勤岗"}],
                {"unknown": "基础管控"},
            )

    def test_release_gate_rejects_unexpected_preview_counts(self):
        with self.assertRaisesRegex(ValueError, "发布门槛"):
            _validate_expected_preview([{
                "position": "社区民警",
                "departments": [{"id": 1}],
            }])


if __name__ == "__main__":
    unittest.main()
