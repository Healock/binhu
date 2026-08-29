import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.qmf_registration import (  # noqa: E402
    QmfPreviewRequest,
    _execute_run_background,
    prepare_qmf_registration,
)
from services.qmf_runs import initial_steps  # noqa: E402
from services.qmf_status import (  # noqa: E402
    QmfLegacyStatus,
    STATUS_COMPLETED_MATCH,
    STATUS_PENDING,
)


def request(path: str) -> Request:
    return Request({"type": "http", "method": "POST", "path": path, "headers": []})


def platform_task() -> dict:
    return {
        "parser_type": "疑似未注销模型三",
        "row_key": "fixture-row",
        "source_id": 9,
        "name": "测试人员甲",
        "identity_number": "11010519491231002X",
        "phone": "13000000000",
        "address": "虚构地址",
        "community": "虚构社区",
        "result": "在吴",
    }


class QmfStatusIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_is_gone_before_any_external_read_or_write(self):
        load_config = AsyncMock()
        legacy_status = AsyncMock()
        preview = AsyncMock()
        create_run = AsyncMock()
        with (
            patch("routers.qmf_registration.load_qmf_config", load_config),
            patch("routers.qmf_registration._legacy_status_for_task", legacy_status),
            patch("routers.qmf_registration.run_guarded_preview", preview),
            patch("routers.qmf_registration._create_prepared_run", create_run),
            patch("routers.qmf_registration.record_admin_audit", AsyncMock()),
        ):
            with self.assertRaises(HTTPException) as raised:
                await prepare_qmf_registration(
                    QmfPreviewRequest(
                        parser_type="疑似未注销模型三",
                        row_key="fixture-row",
                        source_id=9,
                        expected_revision=3,
                    ),
                    request("/api/qmf-registration/prepare"),
                    user={"id": 2, "username": "permission-user"},
                    conn=object(),
                )
        self.assertEqual(raised.exception.status_code, 410)
        self.assertEqual(
            raised.exception.detail["code"],
            "tencent_data_source_disabled",
        )
        load_config.assert_not_awaited()
        legacy_status.assert_not_awaited()
        preview.assert_not_awaited()
        create_run.assert_not_awaited()

    async def test_prepare_does_not_reopen_completed_legacy_registration(self):
        preview = AsyncMock()
        legacy_status = AsyncMock(return_value=QmfLegacyStatus(
            state=STATUS_COMPLETED_MATCH,
            result="在吴",
            matches_platform_result=True,
        ))
        with (
            patch("routers.qmf_registration._legacy_status_for_task", legacy_status),
            patch("routers.qmf_registration.run_guarded_preview", preview),
            patch("routers.qmf_registration.record_admin_audit", AsyncMock()),
        ):
            with self.assertRaises(HTTPException) as raised:
                await prepare_qmf_registration(
                    QmfPreviewRequest(
                        parser_type="疑似未注销模型三",
                        row_key="fixture-row",
                        source_id=9,
                        expected_revision=3,
                    ),
                    request("/api/qmf-registration/prepare"),
                    user={"id": 2, "username": "permission-user"},
                    conn=object(),
                )
        self.assertEqual(raised.exception.status_code, 410)
        self.assertEqual(
            raised.exception.detail["code"],
            "tencent_data_source_disabled",
        )
        legacy_status.assert_not_awaited()
        preview.assert_not_awaited()

    async def test_execute_rechecks_before_first_write_and_stops(self):
        run = {
            "id": 7,
            "parser_type": "疑似未注销模型三",
            "source_id": 9,
            "expected_revision": 3,
            "_expected_row_hash": "a" * 64,
            "steps": initial_steps(),
        }

        class Pool:
            async def acquire(self):
                return object()

            def release(self, _conn):
                return None

        first_write = AsyncMock()

        async def guarded_registration(*, before_write, **_kwargs):
            await before_write(SimpleNamespace())
            await first_write()
            return {"status": "succeeded"}

        set_result = AsyncMock()
        with (
            patch("routers.qmf_registration.db_manager.get_pool", return_value=Pool()),
            patch("routers.qmf_registration._load_run", AsyncMock(return_value=run)),
            patch(
                "routers.qmf_registration.load_qmf_config",
                AsyncMock(return_value=SimpleNamespace(registration_configured=True)),
            ),
            patch(
                "routers.qmf_registration._current_run_source",
                AsyncMock(return_value=(platform_task(), "a" * 64)),
            ),
            patch("routers.qmf_registration._assert_source_unchanged", AsyncMock()),
            patch(
                "routers.qmf_registration._online_writeback_available",
                AsyncMock(return_value=True),
            ),
            patch(
                "routers.qmf_registration._legacy_status_for_task",
                AsyncMock(return_value=QmfLegacyStatus(
                    state=STATUS_COMPLETED_MATCH,
                    result="在吴",
                    matches_platform_result=True,
                )),
            ),
            patch(
                "routers.qmf_registration.run_guarded_registration",
                AsyncMock(side_effect=guarded_registration),
            ),
            patch("routers.qmf_registration._finish_sending_step", AsyncMock()),
            patch("routers.qmf_registration._set_run_result", set_result),
            patch("routers.qmf_registration.record_admin_audit", AsyncMock()),
        ):
            await _execute_run_background(
                7,
                user={"id": 2, "username": "permission-user"},
                audit_fields={},
            )
        first_write.assert_not_awaited()
        self.assertEqual(set_result.await_args.kwargs["status"], "failed")
        self.assertEqual(
            set_result.await_args.kwargs["result_code"],
            "legacy_already_completed",
        )


if __name__ == "__main__":
    unittest.main()
