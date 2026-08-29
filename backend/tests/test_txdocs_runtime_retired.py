import os
import inspect
import unittest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers import auth, spreadsheets, sync
from routers import qmf_registration
from services import local_source
from services import admin_task_queue


class TencentRuntimeRetiredTests(unittest.TestCase):
    def test_local_source_is_permanently_enabled_and_tencent_access_disabled(self):
        self.assertTrue(local_source.local_data_source_enabled())
        self.assertFalse(local_source.tencent_access_enabled())

    def test_legacy_router_handlers_are_explicitly_retired(self):
        spreadsheet_source = inspect.getsource(spreadsheets)
        sync_source = inspect.getsource(sync)
        auth_source = inspect.getsource(auth)
        self.assertIn("腾讯文档数据源已正式下线", spreadsheet_source)
        self.assertIn("腾讯数据源已下线", sync_source)
        self.assertIn("腾讯 OAuth 已下线", auth_source)

    def test_qmf_marker_retry_cannot_write_external_source(self):
        source = inspect.getsource(qmf_registration.retry_qmf_tencent_marker)
        self.assertIn("raise HTTPException(410", source)
        self.assertNotIn("await _append_tencent_marker(", source.split('"""', 1)[0])

    def test_qmf_prepare_and_execute_cannot_be_reenabled_by_environment(self):
        prepare_source = inspect.getsource(qmf_registration.prepare_qmf_registration)
        execute_source = inspect.getsource(qmf_registration.execute_qmf_registration)
        self.assertIn("raise HTTPException(410", prepare_source)
        self.assertIn("raise HTTPException(410", execute_source)
        self.assertNotIn("settings.TXDOCS_ENABLED", prepare_source)
        self.assertNotIn("settings.TXDOCS_ENABLED", execute_source)

    def test_admin_queue_does_not_expose_external_write_queues(self):
        source = inspect.getsource(admin_task_queue.build_admin_task_queue)
        self.assertNotIn("_sync_jobs()", source)
        self.assertNotIn("_writeback_queues()", source)


if __name__ == "__main__":
    unittest.main()
