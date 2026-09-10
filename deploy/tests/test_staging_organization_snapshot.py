import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from deploy.environments.staging_data.codec import Codec, SnapshotError, normalized
from deploy.environments.staging_data.organization import transform


class OrganizationTests(unittest.TestCase):
    def test_early_scan_reports_organization_field(self):
        from deploy.environments.staging_data.control import safe_diagnostics
        rows, codec = self.fixture()
        codec.remember('在岗')
        with self.assertRaisesRegex(SnapshotError, 'source_sensitive_value_detected') as caught:
            transform(rows, codec, {normalized('虚构社区'): 1})
        self.assertEqual(safe_diagnostics(caught.exception.diagnostics)['fields'],
            [{'table': 'PlatformData._grid_members', 'field': 'status', 'count': 1}])

    def fixture(self):
        codec = Codec(b'a' * 32)
        codec.allocate('community', [1])
        codec.allocate('actor', [20])
        rows = {
            'PlatformData._departments': [{'id': 2, 'name': '虚构社区',
                'department_type': 'community', 'community_id': 1, 'is_active': 1}],
            'PlatformData._grid_members': [{'id': 5, 'name': '虚构核查员',
                'community': '虚构社区', 'department_id': 2, 'status': '在岗', 'position': '组员'}],
            'PlatformData._grid_member_department_links': [{'member_id': 5, 'department_id': 2, 'sort_order': 0}],
            'PlatformData._users': [{'id': 20, 'member_id': 5, 'display_name': '虚构核查员'}]}
        return rows, codec

    def test_task_inspector_and_member_have_identical_pseudonyms(self):
        rows, codec = self.fixture()
        tables = transform(rows, codec, {normalized('虚构社区'): 1})
        member = tables['PlatformData._grid_members'][0]
        user = tables['PlatformData._users'][0]
        self.assertEqual(member['name'], codec.text('staff', '虚构核查员', '验证核查员'))
        self.assertEqual(user['member_id'], member['id'])
        self.assertNotIn('password_hash', user)
        self.assertTrue(user['username'].endswith('@staging'))
        self.assertEqual(member['department_id'], tables['PlatformData._departments'][0]['id'])

    def test_missing_and_duplicate_account_relations_fail(self):
        for mode in ('missing', 'duplicate', 'secret'):
            rows, codec = self.fixture()
            if mode == 'missing':
                rows['PlatformData._users'] = []
            elif mode == 'duplicate':
                rows['PlatformData._users'].append(copy.deepcopy(rows['PlatformData._users'][0]))
            else:
                rows['PlatformData._users'][0]['password_hash'] = 'synthetic-not-exportable'
            with self.assertRaises(SnapshotError):
                transform(rows, codec, {normalized('虚构社区'): 1})


if __name__ == '__main__':
    unittest.main()
