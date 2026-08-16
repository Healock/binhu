import asyncio
import json
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.qmf_registration import (  # noqa: E402
    QmfExecuteRequest,
    QmfPreviewRequest,
    _assert_source_unchanged,
    _append_tencent_marker,
    _background_task_finished,
    _claim_run,
    _create_prepared_run,
    _execute_run_background,
    _freeze_unstarted_background_run,
    _run_payload,
    execute_qmf_registration,
    get_qmf_registration_run,
    prepare_qmf_registration,
    retry_qmf_tencent_marker,
)
from routers.mobile_tasks import _qmf_registration_state  # noqa: E402
from services.qmf_runs import (  # noqa: E402
    TENCENT_MARKER,
    ensure_qmf_registration_schema,
    initial_steps,
    serialize_steps,
)
from services.qmf_registration import QmfPreviewError  # noqa: E402


class _Cursor:
    def __init__(self, *, fetchone_values=None, interrupted=None):
        self.fetchone_values = list(fetchone_values or [])
        self.interrupted = list(interrupted or [])
        self.executed = []
        self.rowcount = 0
        self.lastrowid = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(str(sql).split()), params))

    async def fetchone(self):
        return self.fetchone_values.pop(0) if self.fetchone_values else None

    async def fetchall(self):
        return self.interrupted


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.begun = False
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    async def begin(self):
        self.begun = True

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def request(path: str) -> Request:
    return Request({"type": "http", "method": "POST", "path": path, "headers": []})


class QmfRunSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_check_waits_for_transient_projection_refresh(self):
        cursor = _Cursor(fetchone_values=[
            (3, "a" * 64, None, None),
            (3, "a" * 64, None, None),
            (3, "a" * 64, 1, 0),
        ])
        sleep = AsyncMock()
        with patch("routers.qmf_registration.asyncio.sleep", sleep):
            await _assert_source_unchanged(
                _Conn(cursor),
                parser_type="疑似未注销模型三",
                row_key="internal-row-key",
                source_id=9,
                expected_revision=3,
                expected_hash="a" * 64,
            )
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(len(cursor.executed), 3)
        self.assertTrue(all("LEFT JOIN" in sql for sql, _ in cursor.executed))

    async def test_source_check_distinguishes_missing_projection_from_missing_row(self):
        projection_cursor = _Cursor(fetchone_values=[
            (3, "a" * 64, None, None),
            (3, "a" * 64, None, None),
            (3, "a" * 64, None, None),
        ])
        with (
            patch("routers.qmf_registration.asyncio.sleep", AsyncMock()),
            self.assertRaises(QmfPreviewError) as projection_error,
        ):
            await _assert_source_unchanged(
                _Conn(projection_cursor),
                parser_type="疑似未注销模型三",
                row_key="internal-row-key",
                source_id=9,
                expected_revision=3,
                expected_hash="a" * 64,
            )
        self.assertEqual(
            projection_error.exception.code, "source_projection_refreshing"
        )

        with self.assertRaises(QmfPreviewError) as missing_error:
            await _assert_source_unchanged(
                _Conn(_Cursor(fetchone_values=[None])),
                parser_type="疑似未注销模型三",
                row_key="internal-row-key",
                source_id=9,
                expected_revision=3,
                expected_hash="a" * 64,
            )
        self.assertEqual(missing_error.exception.code, "source_missing")

    async def test_schema_stores_only_row_key_digest_and_recovers_sending_step(self):
        steps = initial_steps()
        steps[4]["status"] = "sending"
        cursor = _Cursor(interrupted=[(7, serialize_steps(steps))])
        await ensure_qmf_registration_schema(cursor)
        sql_text = "\n".join(sql for sql, _params in cursor.executed)
        self.assertIn("row_key_digest CHAR(64)", sql_text)
        self.assertIn("idx_qmf_run_row_key", sql_text)
        self.assertNotIn("row_key VARCHAR", sql_text)
        recovery = next(
            params for sql, params in cursor.executed
            if "steps_json=%s" in sql and "process_interrupted" in sql
        )
        recovered_steps = json.loads(recovery[0])
        self.assertEqual(recovered_steps[4]["status"], "uncertain")
        self.assertEqual(recovered_steps[4]["result_code"], "process_interrupted")

    def test_public_run_payload_has_no_business_row_key_or_person_text(self):
        row = (
            1,
            "疑似未注销模型三",
            9,
            3,
            "a" * 64,
            11,
            "prepared",
            serialize_steps(initial_steps()),
            "",
            "b" * 64,
            "image/jpeg",
            123,
            "not_started",
            "",
            None,
            None,
            None,
            None,
            None,
            None,
        )
        payload = _run_payload(row)
        self.assertNotIn("row_key", payload)
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("身份证号", "手机号", "姓名", "地址"):
            self.assertNotIn(forbidden, serialized)

    async def test_prior_execution_attempt_freezes_new_prepare(self):
        uncertain_steps = initial_steps()
        uncertain_steps[4]["status"] = "sending"
        cursor = _Cursor(fetchone_values=[(
            88, "uncertain", serialize_steps(uncertain_steps)
        )])
        conn = _Conn(cursor)
        with self.assertRaises(QmfPreviewError) as raised:
            await _create_prepared_run(
                conn,
                data=QmfPreviewRequest(
                    parser_type="疑似未注销模型三",
                    row_key="sensitive-business-key-used-only-in-memory",
                    source_id=9,
                    expected_revision=3,
                ),
                user={"id": 11},
                expected_hash="a" * 64,
                preview={
                    "upstream_task": {"task_id": "fictional-task"},
                    "photo": {"sha256": "b" * 64, "mime_type": "image/jpeg", "size_bytes": 1},
                },
            )
        self.assertEqual(raised.exception.code, "registration_frozen")
        self.assertFalse(any("INSERT INTO" in sql for sql, _ in cursor.executed))
        duplicate_sql, duplicate_params = cursor.executed[0]
        self.assertIn("row_key_digest=%s", duplicate_sql)
        self.assertIn("upstream_task_digest=%s", duplicate_sql)
        self.assertIn("idempotency_key=%s", duplicate_sql)
        self.assertEqual(len(duplicate_params), 3)

    async def test_failed_run_before_any_write_can_be_manually_reprepared(self):
        failed_steps = initial_steps()
        for item in failed_steps[:4]:
            item["status"] = "succeeded"
        prepared_row = (
            99, "疑似未注销模型三", 9, 3, "a" * 64, 11, "prepared",
            serialize_steps(initial_steps()), "", "b" * 64, "image/jpeg", 123,
            "not_started", "", None, None, None, None, None, None,
        )
        cursor = _Cursor(fetchone_values=[
            (88, "failed", serialize_steps(failed_steps)),
            prepared_row,
        ])
        cursor.lastrowid = 99
        conn = _Conn(cursor)
        run = await _create_prepared_run(
            conn,
            data=QmfPreviewRequest(
                parser_type="疑似未注销模型三",
                row_key="sensitive-business-key-used-only-in-memory",
                source_id=9,
                expected_revision=3,
            ),
            user={"id": 11},
            expected_hash="a" * 64,
            preview={
                "upstream_task": {"task_id": "fictional-task"},
                "photo": {
                    "sha256": "b" * 64,
                    "mime_type": "image/jpeg",
                    "size_bytes": 123,
                },
            },
        )
        self.assertEqual(run["status"], "prepared")
        self.assertTrue(any(
            params == (88,) and "manual_reprepare_after_prewrite_failure" in sql
            for sql, params in cursor.executed
        ))

    async def test_legacy_uncertain_run_without_write_progress_can_be_reprepared(self):
        cursor = _Cursor(fetchone_values=[
            (88, "uncertain", serialize_steps(initial_steps())),
            (
                99, "疑似未注销模型三", 9, 3, "a" * 64, 11, "prepared",
                serialize_steps(initial_steps()), "", "b" * 64, "image/jpeg", 123,
                "not_started", "", None, None, None, None, None, None,
            ),
        ])
        cursor.lastrowid = 99
        run = await _create_prepared_run(
            _Conn(cursor),
            data=QmfPreviewRequest(
                parser_type="疑似未注销模型三",
                row_key="legacy-uncertain-row",
                source_id=9,
                expected_revision=3,
            ),
            user={"id": 11},
            expected_hash="a" * 64,
            preview={
                "upstream_task": {"task_id": "fictional-task"},
                "photo": {"sha256": "b" * 64, "mime_type": "image/jpeg", "size_bytes": 123},
            },
        )
        self.assertEqual(run["status"], "prepared")
        self.assertTrue(any(
            params == (88,) and "manual_reprepare_after_prewrite_failure" in sql
            for sql, params in cursor.executed
        ))

    def test_uncertain_run_without_write_progress_is_recoverable(self):
        row = (
            1, "疑似未注销模型三", 9, 3, "a" * 64, 11, "uncertain",
            serialize_steps(initial_steps()), "source_projection_refreshing",
            "b" * 64, "image/jpeg", 123, "not_started", "", None, None,
            None, None, None, None,
        )
        self.assertTrue(_run_payload(row)["can_reprepare"])

    async def test_claim_is_serialized_and_rejects_duplicate_execution(self):
        run_row = (
            9, "疑似未注销模型三", 19, 3, "a" * 64, 2, "prepared",
            serialize_steps(initial_steps()), "", "b" * 64, "image/jpeg", 123,
            "not_started", "", None, None, None, None, None, None,
        )
        cursor = _Cursor(fetchone_values=[
            (1,),
            run_row,
            None,
            (8, "uncertain"),
        ])
        conn = _Conn(cursor)
        with (
            patch("routers.qmf_registration.qmf_operation_busy", return_value=False),
            patch(
                "routers.qmf_registration._online_writeback_available",
                AsyncMock(return_value=True),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await _claim_run(
                    conn,
                    9,
                    user={"id": 2, "username": "shenshenghua"},
                    config=SimpleNamespace(registration_configured=True),
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(conn.begun)
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)
        sql_text = "\n".join(sql for sql, _params in cursor.executed)
        self.assertIn("GET_LOCK", sql_text)
        self.assertIn("RELEASE_LOCK", sql_text)
        self.assertIn("prior.row_key_digest=current_run.row_key_digest", sql_text)
        self.assertIn(
            "prior.upstream_task_digest=current_run.upstream_task_digest", sql_text
        )
        self.assertIn("prior.idempotency_key=current_run.idempotency_key", sql_text)

    async def test_only_exact_account_can_prepare_or_execute(self):
        preview_request = QmfPreviewRequest(
            parser_type="疑似未注销模型三",
            row_key="internal-row-key",
            source_id=9,
            expected_revision=3,
        )
        with self.assertRaises(HTTPException) as prepare_error:
            await prepare_qmf_registration(
                preview_request,
                request("/api/qmf-registration/prepare"),
                user={"id": 1, "username": "super-admin", "role": "super_admin"},
                conn=None,
            )
        self.assertEqual(prepare_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as execute_error:
            await execute_qmf_registration(
                1,
                QmfExecuteRequest(),
                request("/api/qmf-registration/runs/1/execute"),
                user={"id": 1, "username": "super-admin", "role": "super_admin"},
                conn=None,
            )
        self.assertEqual(execute_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as get_error:
            await get_qmf_registration_run(
                1,
                user={"id": 1, "username": "super-admin", "role": "super_admin"},
                conn=None,
            )
        self.assertEqual(get_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as retry_error:
            await retry_qmf_tencent_marker(
                1,
                request("/api/qmf-registration/runs/1/retry-marker"),
                user={"id": 1, "username": "super-admin", "role": "super_admin"},
                conn=None,
            )
        self.assertEqual(retry_error.exception.status_code, 403)

    async def test_tencent_marker_preserves_note_is_idempotent_and_uses_exact_field(self):
        run = {
            "parser_type": "疑似未注销模型三",
            "source_id": 9,
            "expected_revision": 3,
            "_expected_row_hash": "a" * 64,
        }
        detail = {
            "task": {"conflict": False},
            "sources": [{"id": 9, "revision": 4, "values": {"备注": "原有说明"}}],
        }
        update = AsyncMock(return_value={"message": "ok"})
        with (
            patch("routers.qmf_registration._source_row_key", AsyncMock(return_value="internal-row-key")),
            patch("routers.qmf_registration._mobile_task_detail_data", AsyncMock(return_value=detail)),
            patch("routers.qmf_registration.update_source_fields", update),
        ):
            result = await _append_tencent_marker(
                object(),
                run=run,
                user={"id": 2, "username": "shenshenghua"},
                request_stub=request("/api/qmf-registration/background"),
                strict_source=False,
            )
        self.assertEqual(result, "written")
        kwargs = update.await_args.kwargs
        self.assertEqual(kwargs["changes"], {"备注": f"原有说明；{TENCENT_MARKER}"})
        self.assertEqual(kwargs["allowed_columns"], {"备注"})
        self.assertEqual(kwargs["system_managed_columns"], {"备注"})
        kwargs["current_values_validator"]({"备注": "原有说明"})
        with self.assertRaises(HTTPException):
            kwargs["current_values_validator"]({"备注": "他人已修改"})

        update.reset_mock()
        detail["sources"][0]["values"]["备注"] = f"原有说明；{TENCENT_MARKER}"
        with (
            patch("routers.qmf_registration._source_row_key", AsyncMock(return_value="internal-row-key")),
            patch("routers.qmf_registration._mobile_task_detail_data", AsyncMock(return_value=detail)),
            patch("routers.qmf_registration.update_source_fields", update),
        ):
            result = await _append_tencent_marker(
                object(),
                run=run,
                user={"id": 2, "username": "shenshenghua"},
                request_stub=request("/api/qmf-registration/background"),
                strict_source=False,
            )
        self.assertEqual(result, "already_present")
        update.assert_not_awaited()

    async def test_final_review_failure_never_writes_tencent_marker(self):
        steps = initial_steps()
        steps[7]["status"] = "succeeded"
        steps[8]["status"] = "sending"
        run = {
            "id": 7,
            "parser_type": "疑似未注销模型三",
            "source_id": 9,
            "expected_revision": 3,
            "_expected_row_hash": "a" * 64,
            "steps": steps,
        }

        class _Pool:
            async def acquire(self):
                return object()

            def release(self, _conn):
                return None

        marker = AsyncMock()
        set_result = AsyncMock()
        with (
            patch("routers.qmf_registration.db_manager.get_pool", return_value=_Pool()),
            patch("routers.qmf_registration._load_run", AsyncMock(return_value=run)),
            patch(
                "routers.qmf_registration.load_qmf_config",
                AsyncMock(return_value=SimpleNamespace(registration_configured=True)),
            ),
            patch(
                "routers.qmf_registration._current_run_source",
                AsyncMock(return_value=({"row_key": "internal"}, "a" * 64)),
            ),
            patch("routers.qmf_registration._assert_source_unchanged", AsyncMock()),
            patch(
                "routers.qmf_registration.run_guarded_registration",
                AsyncMock(side_effect=QmfPreviewError(
                    "final_state_unconfirmed", "final", 409, uncertain=True
                )),
            ),
            patch("routers.qmf_registration._finish_sending_step", AsyncMock()),
            patch("routers.qmf_registration._set_run_result", set_result),
            patch("routers.qmf_registration._append_tencent_marker", marker),
            patch("routers.qmf_registration.record_admin_audit", AsyncMock()),
        ):
            await _execute_run_background(
                7,
                user={"id": 2, "username": "shenshenghua"},
                audit_fields={},
            )
        marker.assert_not_awaited()
        self.assertEqual(set_result.await_args.kwargs["status"], "uncertain")

    async def test_background_start_failure_freezes_claimed_run_locally(self):
        cursor = _Cursor(fetchone_values=[(
            "executing", serialize_steps(initial_steps()), "not_started",
        )])
        cursor.rowcount = 1
        conn = _Conn(cursor)

        class _Pool:
            async def acquire(self):
                return conn

            def release(self, released):
                self.released = released

        pool = _Pool()
        audit = AsyncMock()
        with (
            patch("routers.qmf_registration.db_manager.get_pool", return_value=pool),
            patch("routers.qmf_registration.record_admin_audit", audit),
        ):
            await _freeze_unstarted_background_run(
                7,
                user={"id": 2, "username": "shenshenghua"},
                audit_fields={},
            )
        self.assertIs(pool.released, conn)
        self.assertTrue(any(
            params and "background_start_failed" in params
            for _sql, params in cursor.executed
        ))
        audit.assert_awaited_once()

    async def test_cancelled_background_with_write_progress_becomes_uncertain(self):
        steps = initial_steps()
        steps[4]["status"] = "sending"
        cursor = _Cursor(fetchone_values=[(
            "executing", serialize_steps(steps), "not_started",
        )])
        cursor.rowcount = 1
        conn = _Conn(cursor)

        class _Pool:
            async def acquire(self):
                return conn

            def release(self, _conn):
                return None

        with (
            patch("routers.qmf_registration.db_manager.get_pool", return_value=_Pool()),
            patch("routers.qmf_registration.record_admin_audit", AsyncMock()),
        ):
            await _freeze_unstarted_background_run(
                7,
                user={"id": 2, "username": "shenshenghua"},
                audit_fields={},
                result_code="background_task_cancelled",
            )
        update = next(
            (sql, params) for sql, params in cursor.executed
            if "SET status=%s" in sql
        )
        self.assertEqual(update[1][0], "uncertain")
        recovered_steps = json.loads(update[1][2])
        self.assertEqual(recovered_steps[4]["status"], "uncertain")
        self.assertEqual(
            recovered_steps[4]["result_code"], "background_task_cancelled"
        )

    async def test_cancelled_tencent_marker_keeps_qmf_success_and_returns_to_pending(self):
        cursor = _Cursor(fetchone_values=[(
            "succeeded", serialize_steps(initial_steps()), "writing",
        )])
        cursor.rowcount = 1
        conn = _Conn(cursor)

        class _Pool:
            async def acquire(self):
                return conn

            def release(self, _conn):
                return None

        audit = AsyncMock()
        with (
            patch("routers.qmf_registration.db_manager.get_pool", return_value=_Pool()),
            patch("routers.qmf_registration.record_admin_audit", audit),
        ):
            await _freeze_unstarted_background_run(
                7,
                user={"id": 2, "username": "shenshenghua"},
                audit_fields={},
                result_code="background_task_cancelled",
            )
        marker_update = next(
            (sql, params) for sql, params in cursor.executed
            if "tencent_marker_status='pending'" in sql
        )
        self.assertNotIn("SET status=", marker_update[0])
        self.assertEqual(marker_update[1][0], "background_task_cancelled")
        self.assertEqual(audit.await_args.kwargs["result"], "succeeded")
        self.assertEqual(
            audit.await_args.kwargs["detail"]["tencent_marker_status"], "pending"
        )

    async def test_cancelled_task_schedules_local_freeze(self):
        async def wait_forever():
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        freeze = AsyncMock()
        with patch("routers.qmf_registration._freeze_unstarted_background_run", freeze):
            _background_task_finished(
                task,
                run_id=7,
                user={"id": 2, "username": "shenshenghua"},
                audit_fields={},
            )
            await asyncio.sleep(0)
        freeze.assert_awaited_once()
        self.assertEqual(
            freeze.await_args.kwargs["result_code"], "background_task_cancelled"
        )

    async def test_marker_bookkeeping_failure_never_downgrades_qmf_success(self):
        run = {
            "id": 7,
            "parser_type": "疑似未注销模型三",
            "source_id": 9,
            "expected_revision": 3,
            "_expected_row_hash": "a" * 64,
            "steps": initial_steps(),
        }

        class _Pool:
            async def acquire(self):
                return object()

            def release(self, _conn):
                return None

        set_result = AsyncMock()
        marker = AsyncMock()
        audit = AsyncMock()
        with (
            patch("routers.qmf_registration.db_manager.get_pool", return_value=_Pool()),
            patch("routers.qmf_registration._load_run", AsyncMock(return_value=run)),
            patch(
                "routers.qmf_registration.load_qmf_config",
                AsyncMock(return_value=SimpleNamespace(registration_configured=True)),
            ),
            patch(
                "routers.qmf_registration._current_run_source",
                AsyncMock(return_value=({"row_key": "internal"}, "a" * 64)),
            ),
            patch("routers.qmf_registration._assert_source_unchanged", AsyncMock()),
            patch(
                "routers.qmf_registration.run_guarded_registration",
                AsyncMock(return_value={
                    "status": "succeeded",
                    "upstream_task_id": "fictional-task",
                    "photo": {
                        "sha256": "b" * 64,
                        "mime_type": "image/jpeg",
                        "size_bytes": 123,
                    },
                }),
            ),
            patch("routers.qmf_registration._set_run_result", set_result),
            patch(
                "routers.qmf_registration._set_marker_status",
                AsyncMock(side_effect=RuntimeError("fictional database error")),
            ),
            patch("routers.qmf_registration._append_tencent_marker", marker),
            patch("routers.qmf_registration.record_admin_audit", audit),
        ):
            await _execute_run_background(
                7,
                user={"id": 2, "username": "shenshenghua"},
                audit_fields={},
            )
        self.assertEqual(set_result.await_count, 1)
        self.assertEqual(set_result.await_args.kwargs["status"], "succeeded")
        marker.assert_not_awaited()
        self.assertEqual(audit.await_args.kwargs["result"], "success")

    async def test_ordinary_task_viewer_only_receives_success_summary(self):
        cursor = _Cursor(fetchone_values=[
            (77, datetime(2026, 8, 16, 7, 0, 0), "pending"),
        ])
        private_run, feedback = await _qmf_registration_state(
            _Conn(cursor),
            parser_type="疑似未注销模型三",
            sources=[{"id": 9}],
            user={"id": 3, "username": "ordinary-user"},
        )
        self.assertIsNone(private_run)
        self.assertEqual(feedback["run_id"], 77)
        self.assertEqual(feedback["status"], "succeeded")
        serialized = json.dumps(feedback, ensure_ascii=False)
        self.assertNotIn("steps", serialized)
        self.assertNotIn("result_code", serialized)


if __name__ == "__main__":
    unittest.main()
