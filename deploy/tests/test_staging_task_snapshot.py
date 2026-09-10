import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'backend'))
from services.parsers import get_parser
from services.task_workflow import TASK_WORKFLOWS
from deploy.environments.staging_data.codec import Codec, SnapshotError, normalized
from deploy.environments.staging_data.tasks import transform_values, source_record


class TaskSnapshotTests(unittest.TestCase):
    def test_early_scan_reports_task_field(self):
        from deploy.environments.staging_data.control import safe_diagnostics
        parser, values, codec = self.fixture()
        codec.remember('脱敏验证内容')
        with self.assertRaisesRegex(SnapshotError, 'source_sensitive_value_detected') as caught:
            transform_values(parser, TASK_WORKFLOWS['全链条'], values, {normalized('虚构原社区'):1}, codec)
        self.assertEqual(safe_diagnostics(caught.exception.diagnostics)['fields'],
            [{'table':'OnlineData.t_fullchain','field':'研判','count':1}])

    def test_result_category_matching_source_prose_retains_business_meaning(self):
        parser, values, codec = self.fixture()
        values['研判'] = '待登记'
        safe = transform_values(parser, TASK_WORKFLOWS['全链条'], values,
                                {normalized('虚构原社区'): 1}, codec)
        self.assertEqual(safe['核查结果'], '待登记')
        self.assertEqual(safe['研判'], '脱敏验证内容')
        entry = source_record(parser, {'id': 5, 'physical_row': 9, 'revision': 3, 'row_key': 'old'}, safe, codec)
        codec.assert_tables_safe({'OnlineData.t_fullchain': [entry['task']],
                                  'OnlineData._online_source_rows': [entry['source']],
                                  'OnlineData._local_source_records': [entry['local_record']]})


    def fixture(self):
        parser=get_parser('全链条')
        values={field:'' for field in parser.COLUMNS}
        values.update({'社区':'虚构原社区','姓名':'虚构原人员','身份证号':'fictional-id',
            '电话号码':'fictional-phone','地址':'虚构原路101室','下发日期':'2026-09-10',
            '核查结果':'待登记','研判':'虚构原意见'})
        codec=Codec(b'a'*32)
        codec.allocate('community',[1])
        codec.allocate('source',[5])
        codec.allocate('t_fullchain',[9])
        return parser,values,codec

    def test_current_source_and_business_values_share_new_keys(self):
        parser,values,codec=self.fixture()
        safe=transform_values(parser,TASK_WORKFLOWS['全链条'],values,{normalized('虚构原社区'):1},codec)
        self.assertEqual(safe['核查结果'],'待登记')
        self.assertEqual(safe['研判'],'脱敏验证内容')
        item=source_record(parser,{'id':5,'physical_row':9,'revision':3,'row_key':'old'},safe,codec)
        self.assertEqual(item['task']['id'],item['source']['physical_row'])
        self.assertEqual(item['task']['_row_key'],item['local_record']['business_key'])
        self.assertEqual(item['source']['row_hash'],item['local_record']['content_hash'])
        self.assertNotEqual(item['source']['id'],5)

    def test_unknown_result_and_private_extra_field_are_refused(self):
        parser,values,codec=self.fixture()
        values['核查结果']='not a result'
        with self.assertRaises(SnapshotError):transform_values(parser,TASK_WORKFLOWS['全链条'],values,{normalized('虚构原社区'):1},codec)
        parser,values,codec=self.fixture()
        values['private_body']='synthetic'
        with self.assertRaises(SnapshotError):transform_values(parser,TASK_WORKFLOWS['全链条'],values,{normalized('虚构原社区'):1},codec)


if __name__=='__main__':unittest.main()
