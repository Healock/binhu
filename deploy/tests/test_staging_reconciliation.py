import unittest

from deploy.environments.staging_data.reconciliation import summarize


class ReconciliationTests(unittest.TestCase):
    def test_missing_source_is_classified_without_exporting_business_keys(self):
        business = [{'id': 1, '_row_key': 'synthetic-private-a'},
                    {'id': 2, '_row_key': 'synthetic-private-b'},
                    {'id': 3, '_row_key': 'synthetic-private-c'}]
        ledgers = [{'local_task_id': 1, 'business_key': 'synthetic-private-a', 'status': 'archived'},
                   {'local_task_id': 2, 'business_key': 'synthetic-private-b', 'status': 'active'}]
        result = summarize(business, [], ledgers, {'synthetic-private-a'})
        self.assertEqual(result['business_only_count'], 3)
        self.assertEqual(result['business_only_archived_ledger_count'], 1)
        self.assertEqual(result['business_only_active_ledger_count'], 1)
        self.assertEqual(result['business_only_no_ledger_count'], 1)
        self.assertEqual(result['business_only_archive_key_count'], 1)
        self.assertNotIn('synthetic-private', repr(result))

    def test_duplicates_count_records_instead_of_collapsing_them_into_sets(self):
        business = [{'id': 1, '_row_key': 'synthetic-key'}, {'id': 2, '_row_key': 'synthetic-key'}]
        sources = [{'physical_row': 1, 'row_key': 'synthetic-key'},
                   {'physical_row': 1, 'row_key': 'synthetic-key'}]
        result = summarize(business, sources, [], set())
        self.assertEqual(result['business_count'], 2)
        self.assertEqual(result['source_count'], 2)
        self.assertEqual(result['duplicate_business_key_count'], 1)
        self.assertEqual(result['duplicate_source_key_count'], 1)
