import copy
import unittest
from deploy.tests import test_staging_snapshot_build as build_tests
from deploy.environments.staging_data.recovery import reconstruct, PARSER
from deploy.environments.staging_data.codec import SnapshotError
from services.parsers import get_parser


class RecoveryTests(unittest.TestCase):
    def fixture(self):
        _, tables, _, _ = build_tests.BuildTests().recovery_fixture()
        parser = get_parser(PARSER)
        return (parser, tables['OnlineData.' + parser.table_name], [],
                tables['OnlineData._local_source_records'], [2, 1000])

    def test_reserved_historical_ids_and_revision_are_preserved(self):
        args = self.fixture()
        before = copy.deepcopy(args[1:])
        output = reconstruct(*args)
        self.assertEqual(output[0]['id'], 1001)
        self.assertEqual(output[0]['revision'], 7)
        self.assertEqual(args[1:], before)
        # A second measurement of an already-present source adds nothing.
        self.assertEqual(reconstruct(args[0], args[1], output, args[3], args[4]), [])

    def test_reject_other_parser_duplicate_business_and_scope_expansion(self):
        args = list(self.fixture())
        args[0] = get_parser('全链条')
        with self.assertRaisesRegex(SnapshotError, 'source_recovery_parser_not_approved'):
            reconstruct(*args)
        args = list(self.fixture())
        args[1] *= 2
        with self.assertRaisesRegex(SnapshotError, 'duplicate_current_business_key'):
            reconstruct(*args)
        args = list(self.fixture())
        args[1] = [dict(args[1][0], id=i+1, _row_key=str(i)) for i in range(262)]
        with self.assertRaisesRegex(SnapshotError, 'source_recovery_scope_exceeded'):
            reconstruct(*args)

    def test_bad_json_and_partial_duplicate_ledger_never_recover(self):
        args = list(self.fixture())
        args[3][0]['values_json'] = 'invalid-json'
        with self.assertRaisesRegex(SnapshotError, 'source_recovery_ledger_invalid_json'):
            reconstruct(*args)
        args = list(self.fixture())
        args[3].append(dict(args[3][0], business_key='different-key'))
        with self.assertRaisesRegex(SnapshotError, 'source_recovery_ledger_not_unique'):
            reconstruct(*args)
