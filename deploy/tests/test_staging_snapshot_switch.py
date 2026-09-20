import unittest
from unittest.mock import patch

from deploy.environments.staging_data import switch_control
from deploy.environments.staging_data.codec import SnapshotError


class StagingSnapshotSwitchTests(unittest.TestCase):
    def test_health_requires_three_consecutive_successes(self):
        responses = [OSError(), {'health': True}, {'health': True}, {'health': True}]
        def probe(_version):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        with patch.object(switch_control, '_probe', side_effect=probe) as call, \
                patch.object(switch_control.time, 'sleep'):
            self.assertTrue(switch_control._wait_stable('1.2.3')['health'])
        self.assertEqual(call.call_count, 4)

    def test_three_consecutive_failures_trigger_rollback_gate(self):
        with patch.object(switch_control, '_probe', side_effect=OSError), \
                patch.object(switch_control.time, 'sleep'):
            with self.assertRaisesRegex(SnapshotError, 'staging_health_failed_three_times'):
                switch_control._wait_stable('1.2.3')

    def test_snapshot_error_is_counted_before_three_successes(self):
        responses = [SnapshotError('critical'), {'health': True}, {'health': True}, {'health': True}]
        def probe(_version):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        with patch.object(switch_control, '_probe', side_effect=probe) as call, \
                patch.object(switch_control.time, 'sleep'):
            self.assertTrue(switch_control._wait_stable('1.2.3')['health'])
        self.assertEqual(call.call_count, 4)

    def test_candidate_verification_requires_all_switch_gates(self):
        good = {'ready_for_application_switch': True, 'sensitive_value_matches': 0,
                'reference_integrity': True, 'row_counts_verified': True,
                'idempotent_import': True, 'pending_gates': []}
        with patch.object(switch_control, 'candidate_execute', return_value=good):
            self.assertEqual(switch_control._candidate_is_verified('staging-'+'a'*16), good)
        for key, value in [('ready_for_application_switch', False), ('sensitive_value_matches', 1),
                           ('reference_integrity', False), ('row_counts_verified', False),
                           ('idempotent_import', False), ('pending_gates', ['x'])]:
            bad = dict(good); bad[key] = value
            with self.subTest(key=key), patch.object(switch_control, 'candidate_execute', return_value=bad):
                with self.assertRaisesRegex(SnapshotError, 'verification_incomplete'):
                    switch_control._candidate_is_verified('staging-'+'a'*16)


if __name__ == '__main__':
    unittest.main()
