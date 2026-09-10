import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault('MYSQL_PASSWORD', 'test-password')
os.environ.setdefault('ENCRYPTION_KEY', 'test-key')
from config import settings
from services import environment_runtime as runtime

DOMAINS = ('ONLINE_DATA', 'ARCHIVE', 'DAILY_REPORT', 'PLATFORM', 'VISIT', 'DISPATCH', 'REGISTRY', 'WORKFLOW')


class DatabaseMarkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = settings.model_copy()
        self.config.APP_ENVIRONMENT = 'staging'
        self.config.MYSQL_HOST = 'environment-mysql'
        self.config.MYSQL_USER = 'environment_app'
        self.config.TXDOCS_ENABLED = False
        self.config.LOCAL_DATA_SOURCE_ENABLED = True
        for name in ('QMF_SOURCE_ACQUISITION_ENABLED', 'QMF_REGISTRATION_ENABLED', 'VENUE_CLOUD_SYNC_ENABLED', 'VENUE_CLOUD_PULL_ENABLED'):
            setattr(self.config, name, False)
        for domain in DOMAINS:
            setattr(self.config, 'MYSQL_' + domain + '_DB', 'Staging_' + domain)
        self.cursor = AsyncMock()
        self.cursor.__aenter__.return_value = self.cursor
        self.cursor.fetchone.return_value = ('staging',)
        self.connection = Mock()
        self.connection.cursor.return_value = self.cursor

    async def verify(self):
        with patch.object(runtime, 'settings', self.config), patch.object(runtime.aiomysql, 'connect', new=AsyncMock(return_value=self.connection)):
            await runtime.verify_database_identity()

    async def test_verifies_all_eight_domains(self):
        await self.verify()
        queries = [c.args[0] for c in self.cursor.execute.call_args_list]
        self.assertEqual(len(queries), 8)
        for domain in DOMAINS:
            self.assertTrue(any('Staging_' + domain + '`' in q for q in queries))
        self.connection.close.assert_called_once()

    async def test_wrong_marker_in_last_domain_fails(self):
        self.cursor.fetchone.side_effect = [('staging',)] * 7 + [('production',)]
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            await self.verify()
        self.connection.close.assert_called_once()

    async def test_missing_marker_fails(self):
        self.cursor.fetchone.side_effect = [('staging',), None]
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            await self.verify()
        self.connection.close.assert_called_once()

    async def test_missing_table_closes_connection(self):
        self.cursor.execute.side_effect = RuntimeError('synthetic missing table')
        with self.assertRaises(RuntimeError):
            await self.verify()
        self.connection.close.assert_called_once()

    async def test_invalid_identifier_rejected_before_connect(self):
        self.config.MYSQL_WORKFLOW_DB = 'Staging_bad`name'
        with patch.object(runtime, 'settings', self.config), patch.object(runtime.aiomysql, 'connect', new=AsyncMock()) as connect:
            with self.assertRaises(ValueError):
                await runtime.verify_database_identity()
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
