import copy
import unittest
from deploy.tests import test_staging_snapshot_build as build_tests
from deploy.environments.staging_data.recovery import reconstruct, recovery_scope, PARSER
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

    def test_allowed_source_kind_is_retained_in_recovered_row(self):
        args = list(self.fixture())
        args[3][0]['source_kind'] = 'local_dispatch'
        output = reconstruct(*args)
        self.assertEqual(output[0]['source_kind'], 'local_dispatch')

    def test_reject_other_parser_and_duplicate_business(self):
        args = list(self.fixture())
        args[0] = get_parser('全链条')
        with self.assertRaisesRegex(SnapshotError, 'source_recovery_parser_not_approved'):
            reconstruct(*args)
        args = list(self.fixture())
        args[1] *= 2
        with self.assertRaisesRegex(SnapshotError, 'duplicate_current_business_key'):
            reconstruct(*args)
    def test_scope_is_deterministic_and_excludes_only_aggregate_evidence(self):
        args = list(self.fixture())
        parser = args[0]
        args[1] = [dict(args[1][0], id=i + 1, _row_key=str(i)) for i in range(300)]
        selected, scope = recovery_scope(parser, args[1], args[2], limit=261,
                                         community_digest=lambda value: 'community-hmac')
        self.assertEqual(len(selected), 261)
        self.assertEqual(scope['candidate_missing_count'], 300)
        self.assertEqual(scope['recovered_count'], 261)
        self.assertEqual(scope['excluded_count'], 39)
        self.assertEqual(scope['excluded_by_community'][0]['community_key'], 'community-hmac')
        self.assertEqual(set(scope), {
            'parser_type', 'maximum_recovered_sources', 'candidate_missing_count',
            'recovered_count', 'excluded_count', 'excluded_date_min',
            'excluded_date_max', 'excluded_by_community',
        })

    def test_known_business_key_collision_is_excluded_before_sampling(self):
        args = list(self.fixture())
        parser = args[0]
        existing = dict(args[1][0], id=9001, _row_key=args[1][0]['_row_key'])
        args[2] = [{'parser_type': PARSER, 'physical_row': existing['id'],
                    'row_key': existing['_row_key']}]
        selected, scope = recovery_scope(parser, [args[1][0], dict(args[1][0], id=2, _row_key='other')], args[2], limit=10, sample_mode=True, sample_limit=10)
        self.assertEqual([row['_row_key'] for row in selected], ['other'])
        self.assertEqual(scope['excluded_by_reason'], {'existing_business_key_collision': 1})

    def test_sampling_covers_multiple_strata_deterministically(self):
        args = list(self.fixture())
        parser = args[0]
        rows = []
        for index in range(12):
            rows.append(dict(args[1][0], id=index + 1, _row_key=f'key-{index}',
                             **{'下发社区': f'社区{index % 3}', '截止时间': f'2026-09-{index + 1:02d}',
                                 '核查结果': ('已登记' if index % 2 else '离苏'),
                                 '核查人': ('核查员' if index % 2 else '')}))
        selected, _ = recovery_scope(parser, rows, [], limit=6)
        self.assertEqual(len(selected), 6)
        self.assertEqual([row['id'] for row in selected], [1, 2, 3, 4, 5, 6])

    def test_bad_json_and_partial_duplicate_ledger_never_recover(self):
        args = list(self.fixture())
        args[3][0]['values_json'] = 'invalid-json'
        with self.assertRaisesRegex(SnapshotError, 'source_recovery_ledger_invalid_json'):
            reconstruct(*args)
        args = list(self.fixture())
        args[3].append(dict(args[3][0], business_key='different-key'))
        with self.assertRaisesRegex(SnapshotError, 'source_recovery_ledger_not_unique'):
            reconstruct(*args)

    def test_approved_ledger_conflicts_are_excluded_with_aggregate_diagnostics(self):
        args = list(self.fixture())
        args[3].clear()
        recovered, diagnostics = reconstruct(*args, exclude_approved_conflicts=True,
                                              return_diagnostics=True,
                                              community_digest=lambda value: 'community-hmac')
        self.assertEqual(recovered, [])
        self.assertEqual(diagnostics['conflict_count'], 1)
        self.assertEqual(diagnostics['conflict_by_type'], {'missing_active_ledger': 1})
        self.assertEqual(diagnostics['conflict_by_community'], [{'community_key': 'community-hmac', 'count': 1}])

    def test_unapproved_ledger_value_mismatch_still_fails(self):
        args = list(self.fixture())
        args[3][0]['content_hash'] = 'bad'
        with self.assertRaisesRegex(SnapshotError, 'source_recovery_ledger_mismatch'):
            reconstruct(*args, exclude_approved_conflicts=True)
