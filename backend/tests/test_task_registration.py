import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.task_registration import (
    REGISTRATION_TASK_TYPES,
    address_hmac,
    ensure_missing_registration_review,
    finalize_registration_writeback,
    is_pending_registration,
    is_registration_task,
    mark_registration_confirmation_failed,
    registration_task_state,
    registration_context_reason,
    refresh_registration_source_context_after_writeback,
    select_registration_property,
    update_registration_match,
)
from services.residence_status_scan import registration_address_match_result


class RegistrationMatchCursor:
    def __init__(self):
        self.execute = AsyncMock(side_effect=self._execute)
        self.status = "awaiting_match"
        self.reason_code = ""
        self.match_count = 0
        self.last_address_hmac = ""
        self.last_scan_token = ""
        self.rowcount = 0

    async def _execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        if normalized.startswith("UPDATE _task_registration_links"):
            self.status = str(params[0])
            self.reason_code = str(params[1])
            self.match_count = int(params[2])
            self.last_address_hmac = str(params[3])
            self.last_scan_token = str(params[4])
            self.rowcount = 1

    def link(self):
        return {
            "source_id": 11,
            "property_id": 23,
            "match_count": self.match_count,
            "last_address_hmac": self.last_address_hmac,
            "last_scan_token": self.last_scan_token,
        }


class TaskRegistrationRulesTests(unittest.TestCase):
    def test_only_six_businesses_use_property_registration_closure(self):
        self.assertEqual(
            set(REGISTRATION_TASK_TYPES),
            {
                "全链条",
                "出租房屋核查",
                "寄递业",
                "疑似返苏",
                "苏州涉警",
                "交通涉警",
            },
        )
        self.assertFalse(is_registration_task("疑似未注销模型三"))

    def test_pending_registration_uses_each_business_result_field(self):
        self.assertTrue(is_pending_registration("全链条", {"核查结果": "待登记"}))
        self.assertTrue(is_pending_registration("疑似返苏", {"核查反馈": "待登记"}))
        self.assertFalse(is_pending_registration("疑似返苏", {"核查结果": "待登记"}))
        self.assertFalse(is_pending_registration("疑似未注销模型三", {"核查结果": "待登记"}))

    def test_new_pending_registration_is_checked_but_legacy_row_stays_completed(self):
        values = {"核查结果": "待登记"}
        self.assertEqual(registration_task_state("全链条", values), "checked")
        self.assertEqual(
            registration_task_state("全链条", values, "legacy_completed"),
            "completed",
        )
        self.assertEqual(
            registration_task_state("全链条", values, "confirmed"),
            "completed",
        )

    def test_address_hmac_is_stable_without_exposing_plaintext(self):
        first = address_hmac("虚构路 1 号 101 室")
        second = address_hmac("虚构路1号101室")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotIn("虚构路", first)
        self.assertEqual(address_hmac(""), "")

    def test_exact_address_match_distinguishes_mismatch_and_ambiguity(self):
        self.assertEqual(
            registration_address_match_result(
                "虚构路1号101室",
                matching_property_count=1,
                other_property_count=0,
            ),
            (True, ""),
        )

    def test_stale_source_and_property_context_stop_automatic_confirmation(self):
        base = {
            "source_count": 1,
            "source_id": 11,
            "current_source_id": 11,
            "source_revision": 4,
            "current_source_revision": 4,
            "source_row_hash": "a" * 64,
            "current_source_row_hash": "a" * 64,
            "identity_hmac": "b" * 64,
            "current_identity_hmac": "b" * 64,
            "task_community": "长板",
            "current_community": "长板",
            "property_status": "active",
            "property_version": 3,
            "current_version": 3,
        }
        self.assertEqual(registration_context_reason(base), "")
        self.assertEqual(
            registration_context_reason({**base, "source_count": 2}),
            "source_ambiguous",
        )
        self.assertEqual(
            registration_context_reason({**base, "current_source_revision": 5}),
            "source_changed",
        )
        self.assertEqual(
            registration_context_reason({**base, "current_community": "龙河"}),
            "community_conflict",
        )
        self.assertEqual(
            registration_context_reason({**base, "current_version": 4}),
            "property_changed",
        )
        self.assertEqual(
            registration_address_match_result(
                "虚构路1号101室",
                matching_property_count=2,
                other_property_count=1,
            ),
            (False, "address_ambiguous"),
        )
        self.assertEqual(
            registration_address_match_result(
                "",
                matching_property_count=0,
                other_property_count=0,
            ),
            (False, "address_mismatch"),
        )


class TaskRegistrationMatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_writeback_confirms_only_matching_source_context(self):
        cursor = type(
            "Cursor",
            (),
            {
                "execute": AsyncMock(),
                "fetchone": AsyncMock(return_value=(23, "confirmation_pending", 6, "b" * 64, 6, "b" * 64)),
            },
        )()

        await finalize_registration_writeback(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            source_id=11,
            succeeded=True,
        )

        update_sql, update_params = cursor.execute.await_args_list[1].args
        self.assertIn("COALESCE(confirmed_at,UTC_TIMESTAMP())", update_sql)
        self.assertEqual(update_params[:3], ("confirmed", "", "confirmed"))

    async def test_successful_writeback_with_stale_source_context_requires_review(self):
        cursor = type(
            "Cursor",
            (),
            {
                "execute": AsyncMock(),
                "fetchone": AsyncMock(return_value=(23, "confirmation_pending", 5, "a" * 64, 6, "b" * 64)),
            },
        )()

        await finalize_registration_writeback(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            source_id=11,
            succeeded=True,
        )

        update_sql, update_params = cursor.execute.await_args_list[1].args
        self.assertIn("confirmed_at=IF", update_sql)
        self.assertEqual(update_params[:3], ("review_required", "source_changed", "review_required"))
        event_params = cursor.execute.await_args_list[2].args[1]
        self.assertEqual(event_params[4], "registration_writeback_failed")
        self.assertEqual(event_params[5], "source_changed")

    async def test_confirmation_enqueue_failure_returns_pending_state_to_review(self):
        cursor = type("Cursor", (), {"execute": AsyncMock(), "rowcount": 1})()

        changed = await mark_registration_confirmation_failed(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            source_id=11,
            property_id=23,
        )

        self.assertTrue(changed)
        reset_sql = cursor.execute.await_args_list[0].args[0]
        self.assertIn("status='review_required'", reset_sql)
        self.assertIn("'confirmation_pending'", reset_sql)
        self.assertEqual(
            cursor.execute.await_args_list[0].args[1][0],
            "confirmation_enqueue_failed",
        )

    async def test_expected_writeback_advances_registration_source_context(self):
        cursor = type("Cursor", (), {"execute": AsyncMock(), "rowcount": 1})()

        changed = await refresh_registration_source_context_after_writeback(
            cursor,
            parser_type="全链条",
            source_id=11,
            previous_revision=5,
            previous_row_hash="a" * 64,
            current_revision=6,
            current_row_hash="b" * 64,
        )

        self.assertTrue(changed)
        sql, params = cursor.execute.await_args.args
        self.assertIn("source_revision=%s,source_row_hash=%s", sql)
        self.assertIn("source_revision=%s AND source_row_hash=%s", sql)
        self.assertEqual(params[:2], (6, "b" * 64))
        self.assertEqual(params[-2:], (5, "a" * 64))

    async def test_reselecting_same_property_after_review_refreshes_source_context(self):
        cursor = type(
            "Cursor",
            (),
            {
                "execute": AsyncMock(),
                "fetchone": AsyncMock(
                    return_value=(
                        11,
                        23,
                        3,
                        "review_required",
                        4,
                        "a" * 64,
                        "b" * 64,
                        "长板",
                    )
                ),
            },
        )()

        await select_registration_property(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            source_id=11,
            property_id=23,
            property_version=3,
            source_revision=5,
            source_row_hash="c" * 64,
            identity_hmac="b" * 64,
            task_community="长板",
            user_id=7,
        )

        self.assertEqual(cursor.execute.await_count, 3)
        reset_sql = cursor.execute.await_args_list[1].args[0]
        self.assertIn("status='awaiting_match'", reset_sql)

    async def test_external_pending_result_creates_missing_property_review(self):
        cursor = type("Cursor", (), {"execute": AsyncMock()})()

        await ensure_missing_registration_review(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            values={"核查结果": "待登记"},
            source_contexts=[{"id": 11, "revision": 4, "row_hash": "a" * 64}],
            identity_hmac="b" * 64,
            task_community="长板",
        )

        cursor.execute.assert_awaited_once()
        sql, params = cursor.execute.await_args.args
        self.assertIn("'review_required'", sql)
        self.assertIn("status='cancelled'", sql)
        self.assertEqual(params[2:5], (11, 4, "a" * 64))
        self.assertEqual(params[-1], "missing_property")

    async def test_non_pending_result_does_not_create_review_marker(self):
        cursor = type("Cursor", (), {"execute": AsyncMock()})()

        await ensure_missing_registration_review(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            values={"核查结果": "离苏"},
            source_contexts=[],
            identity_hmac="",
            task_community="长板",
        )

        cursor.execute.assert_not_awaited()

    async def test_two_distinct_cycles_with_same_address_are_required(self):
        cursor = RegistrationMatchCursor()
        address_key = "a" * 64

        first = await update_registration_match(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            link=cursor.link(),
            scan_token="cycle-1",
            matched=True,
            observed_address_hmac=address_key,
        )
        self.assertFalse(first)
        self.assertEqual(cursor.status, "matched_once")
        self.assertEqual(cursor.match_count, 1)

        duplicate_cycle = await update_registration_match(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            link=cursor.link(),
            scan_token="cycle-1",
            matched=True,
            observed_address_hmac=address_key,
        )
        self.assertFalse(duplicate_cycle)
        self.assertEqual(cursor.match_count, 1)

        second = await update_registration_match(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            link=cursor.link(),
            scan_token="cycle-2",
            matched=True,
            observed_address_hmac=address_key,
        )
        self.assertTrue(second)
        # The second independent match only enters the writeback stage.  The
        # task is finalized as 已登记 after the queued Tencent write succeeds.
        self.assertEqual(cursor.status, "confirmation_pending")
        self.assertEqual(cursor.match_count, 2)

    async def test_changed_address_does_not_count_as_a_consecutive_match(self):
        cursor = RegistrationMatchCursor()
        await update_registration_match(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            link=cursor.link(),
            scan_token="cycle-1",
            matched=True,
            observed_address_hmac="a" * 64,
        )

        confirmed = await update_registration_match(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            link=cursor.link(),
            scan_token="cycle-2",
            matched=True,
            observed_address_hmac="b" * 64,
        )

        self.assertFalse(confirmed)
        self.assertEqual(cursor.status, "matched_once")
        self.assertEqual(cursor.match_count, 1)

    async def test_mismatch_resets_match_count_and_records_safe_reason(self):
        cursor = RegistrationMatchCursor()
        cursor.match_count = 1
        cursor.last_address_hmac = "a" * 64
        cursor.last_scan_token = "cycle-1"

        confirmed = await update_registration_match(
            cursor,
            parser_type="全链条",
            row_key="row-key",
            link=cursor.link(),
            scan_token="cycle-2",
            matched=False,
            reason_code="address_mismatch",
        )

        self.assertFalse(confirmed)
        self.assertEqual(cursor.status, "review_required")
        self.assertEqual(cursor.match_count, 0)
        self.assertEqual(cursor.reason_code, "address_mismatch")


class RegistrationIntegrationContractTests(unittest.TestCase):
    def test_registry_community_name_lookup_uses_online_schema_prefix(self):
        source = Path(__file__).parents[1].joinpath("routers", "registry.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("settings.MYSQL_ONLINE_DATA_DB", source)
        self.assertIn("`{online_schema}`._communities", source)
        self.assertIn("`{online_schema}`._community_aliases", source)

    def test_projection_persists_external_pending_rows_for_registration_review(self):
        source = Path(__file__).parents[1].joinpath(
            "services", "online_source.py"
        ).read_text(encoding="utf-8")
        self.assertIn("ensure_missing_registration_review", source)


if __name__ == "__main__":
    unittest.main()
