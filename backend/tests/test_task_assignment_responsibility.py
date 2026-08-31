import os
import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.task_assignment_responsibility import (
    capture_first_assignment,
    record_internal_transfer,
    resolve_first_assignment_candidate,
    task_update_is_credited_to,
)


class FirstAssignmentResolutionTests(unittest.TestCase):
    def test_explicit_first_assignment_event_wins_over_later_ledger_owner(self):
        resolved, reason = resolve_first_assignment_candidate(
            [{
                "occurred_at": datetime(2026, 8, 1, 9, 0),
                "community": "长板社区",
                "inspector": "第一核查人",
            }],
            [{
                "occurred_at": date(2026, 8, 10),
                "community": "冬梅社区",
                "inspector": "接手组长",
            }],
        )

        self.assertEqual(reason, "")
        self.assertEqual(resolved["community"], "长板社区")
        self.assertEqual(resolved["inspector"], "第一核查人")
        self.assertEqual(resolved["capture_source"], "migration_assignment_event")

    def test_earliest_daily_ledger_is_safe_fallback(self):
        resolved, reason = resolve_first_assignment_candidate(
            [],
            [
                {
                    "occurred_at": date(2026, 8, 5),
                    "community": "冬梅社区",
                    "inspector": "后续人员",
                },
                {
                    "occurred_at": date(2026, 8, 2),
                    "community": "长板社区",
                    "inspector": "第一核查人",
                },
            ],
        )

        self.assertEqual(reason, "")
        self.assertEqual(resolved["community"], "长板社区")
        self.assertEqual(resolved["inspector"], "第一核查人")
        self.assertEqual(resolved["capture_source"], "migration_daily_ledger")

    def test_conflicting_earliest_events_are_not_guessed(self):
        occurred_at = datetime(2026, 8, 1, 9, 0)
        resolved, reason = resolve_first_assignment_candidate(
            [
                {
                    "occurred_at": occurred_at,
                    "community": "长板社区",
                    "inspector": "人员甲",
                },
                {
                    "occurred_at": occurred_at,
                    "community": "冬梅社区",
                    "inspector": "人员乙",
                },
            ],
            [],
        )

        self.assertIsNone(resolved)
        self.assertEqual(reason, "assignment_event_conflict")

    def test_current_assignment_is_never_used_without_history(self):
        resolved, reason = resolve_first_assignment_candidate([], [])

        self.assertIsNone(resolved)
        self.assertEqual(reason, "history_missing")


class FirstAssignmentPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_requires_both_community_and_inspector(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()

        self.assertFalse(await capture_first_assignment(
            cursor,
            parser_type="全链条",
            row_key="row-1",
            community="",
            inspector="组员甲",
        ))
        self.assertFalse(await capture_first_assignment(
            cursor,
            parser_type="全链条",
            row_key="row-1",
            community="长板",
            inspector="",
        ))
        cursor.execute.assert_not_awaited()

    async def test_capture_uses_insert_ignore_and_never_overwrites_first_owner(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.rowcount = 0

        inserted = await capture_first_assignment(
            cursor,
            parser_type="全链条",
            row_key="row-1",
            community="长板",
            inspector="第一核查人",
        )

        self.assertFalse(inserted)
        sql, params = cursor.execute.await_args.args
        self.assertIn("INSERT IGNORE", sql)
        self.assertNotIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertEqual(params[2:4], ("长板", "第一核查人"))

    async def test_internal_transfer_never_guesses_missing_first_owner(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=None)

        with self.assertRaisesRegex(LookupError, "第一核查人责任记录"):
            await record_internal_transfer(
                cursor,
                parser_type="全链条",
                row_key="row-1",
                source_id=11,
                before={"社区": "长板社区", "核查人": "当前核查人"},
                target_community="冬梅社区",
                target_leader="接手组长",
                operator_user_id=9,
                source_revision=4,
            )

        self.assertEqual(cursor.execute.await_count, 1)
        self.assertIn("FOR UPDATE", cursor.execute.await_args.args[0])

    async def test_workload_is_only_credited_to_recorded_first_inspector(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(side_effect=[None, ("第一核查人",), ("第一核查人",)])

        self.assertFalse(await task_update_is_credited_to(
            cursor, "全链条", "row-1", "第一核查人",
        ))
        self.assertTrue(await task_update_is_credited_to(
            cursor, "全链条", "row-1", "第一核查人",
        ))
        self.assertFalse(await task_update_is_credited_to(
            cursor, "全链条", "row-1", "接手组长",
        ))


if __name__ == "__main__":
    unittest.main()
