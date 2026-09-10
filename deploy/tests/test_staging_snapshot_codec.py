import unittest
from deploy.environments.staging_data.codec import Codec, SnapshotError, enum, date_value
from deploy.environments.staging_data.tasks import business_date


class SnapshotCodecTests(unittest.TestCase):
    def test_source_category_requires_flat_text_values_in_both_source_tables(self):
        import json
        from services.parsers import get_parser
        codec = Codec(b'a' * 32)
        codec.remember('待登记')
        values = {field: '' for field in get_parser('全链条').COLUMNS}
        values.update({'核查结果': '待登记', '研判': {'nested': 'synthetic'}})
        for table in ('OnlineData._online_source_rows', 'OnlineData._local_source_records'):
            with self.subTest(table=table), self.assertRaisesRegex(SnapshotError, 'task_value_contract_mismatch'):
                codec.assert_tables_safe({table: [{'parser_type': '全链条',
                                                  'values_json': json.dumps(values)}]})

    def test_result_contract_never_exempts_other_fields_or_unknown_values(self):
        import json
        codec = Codec(b'a' * 32)
        codec.remember('待登记')
        self.assertEqual(codec.scan({'核查结果': '待登记'}), 1)
        for table, row in (
            ('OnlineData.t_fullchain', {'研判': '待登记'}),
            ('OnlineData.t_fullchain', {'研判': json.dumps({'核查结果': '待登记'})}),
            ('OnlineData._online_source_rows', {'values_json': json.dumps({'核查结果': '待登记'})}),
        ):
            with self.subTest(table=table), self.assertRaises(SnapshotError):
                codec.assert_tables_safe({table: [row]})
        with self.assertRaises(SnapshotError):
            codec.assert_tables_safe({'OnlineData.t_fullchain': [{'核查结果': 'arbitrary-note'}]})

    def test_late_sensitive_value_in_embedded_json_still_blocks_final_scan(self):
        import json
        from deploy.environments.staging_data.control import safe_diagnostics
        codec = Codec(b'a' * 32)
        tables = {'OnlineData._online_source_rows': [
            {'values_json': json.dumps({'nested': ['synthetic-private', 'synthetic-private']})}]}
        codec.assert_tables_safe(tables)
        codec.remember('synthetic-private')
        with self.assertRaises(SnapshotError) as caught:
            codec.assert_tables_safe(tables)
        self.assertEqual(safe_diagnostics(caught.exception.diagnostics), {
            'match_count': 2, 'fields': [{'table': 'OnlineData._online_source_rows',
                                        'field': 'values_json', 'count': 2}]})
        self.assertNotIn('synthetic-private', repr(caught.exception.diagnostics))

    def test_snapshot_stability_and_different_salts(self):
        a,b = Codec(b'a'*32), Codec(b'b'*32)
        self.assertEqual(a.address(1,'验证原始Ａ 1'), a.address(1,'验证原始A1'))
        self.assertNotEqual(a.address(1,'虚构地址'),a.address(2,'虚构地址'))
        self.assertNotEqual(a.phone('synthetic-phone'),b.phone('synthetic-phone'))

    def test_primary_keys_are_remapped_and_references_are_closed(self):
        a=Codec(b'a'*32)
        a.allocate('community',[1,2,3])
        ids={a.reference('community',x) for x in [1,2,3]}
        self.assertEqual(len(ids),3)
        self.assertFalse(ids.intersection({1,2,3}))
        with self.assertRaises(SnapshotError):a.reference('community',4)
        self.assertIsNone(a.reference('community',None))

    def test_phone_identity_shape_and_no_source_value_retained(self):
        a=Codec(b'a'*32)
        phone=a.phone('fictional-phone')
        identity=a.identity('fictional-id')
        self.assertRegex(phone,r'^199\d{8}$')
        self.assertRegex(identity,r'^990000\d{11}[0-9X]$')
        self.assertEqual(a.identity('fictional-id'),identity)
        self.assertEqual(a.scan({'phone':phone,'identity':identity}),0)
        self.assertEqual(a.scan({'payload':['fictional-id']}),1)

    def test_unknown_status_and_unstructured_dates_fail_closed(self):
        with self.assertRaises(SnapshotError):enum('private text',{'active'})
        with self.assertRaises(SnapshotError):date_value('unreviewed source body')
        self.assertEqual(date_value('2026-09-10'),'2026-09-10')

    def test_local_import_date_formats_are_normalized(self):
        self.assertEqual(date_value('2026/9/8'), '2026-09-08')
        self.assertEqual(date_value('2026/09/08 17:20:00'), '2026-09-08 17:20:00')
        self.assertEqual(date_value('2026-09-08T17:20:00'), '2026-09-08 17:20:00')
        self.assertEqual(date_value('2026-09-08T17:20:00+08:00'), '2026-09-08 17:20:00+08:00')

    def test_business_date_formats_are_explicit_and_fail_closed(self):
        self.assertEqual(business_date('2026-09-08'), '2026-09-08')
        self.assertEqual(business_date('2026/9/8'), '2026/9/8')
        self.assertEqual(business_date('2026-09-08 17:20:00'), '2026-09-08 17:20:00')
        with self.assertRaises(SnapshotError):
            business_date('2026-09-08 备注正文')
        with self.assertRaises(SnapshotError):
            business_date('2026-02-30')

    def test_impossible_calendar_date_is_rejected(self):
        with self.assertRaises(SnapshotError):date_value('2026/02/30')

    def test_dispatch_month_day_retains_precision_without_guessing_year(self):
        for value in ('09-08', '9-8', '02-29', '2.29'):
            with self.subTest(value=value):
                self.assertEqual(business_date(value), value)
        for value in ('02-30', '13-01', '09-08 synthetic-note', '2025-02-29', '2026-09-08T99:00:00'):
            with self.subTest(value=value), self.assertRaises(SnapshotError):
                business_date(value)
        for value in (None, '', '  '):
            self.assertEqual(business_date(value), '')
        self.assertEqual(business_date('2026-09-08T17:20:00'), '2026-09-08T17:20:00')

    def test_closed_housing_enum_does_not_mask_sensitive_text_elsewhere(self):
        a = Codec(b'a' * 32)
        value = '自购房屋'
        a.remember(value)
        tables = {
            'RegistryData.registry_properties': [{'housing_type': value, 'residence_type': value}],
        }
        self.assertEqual(a.scan_tables(tables), 1)

    def test_closed_housing_enum_rejects_unknown_value(self):
        a = Codec(b'a' * 32)
        with self.assertRaises(SnapshotError):
            a.scan_tables({'RegistryData.registry_properties': [{'housing_type': '任意正文'}]})

    def test_sensitive_scan_summary_contains_only_table_field_and_count(self):
        a = Codec(b'a' * 32)
        a.remember('source-value')
        summary = a.scan_table_summary({'OnlineData.t_fullchain': [{'备注': 'source-value'}]})
        self.assertEqual(summary, [{'table': 'OnlineData.t_fullchain', 'field': '备注', 'count': 1}])


if __name__=='__main__':unittest.main()

# Keep the dispatch date contract covered by the PR synchronization check.

