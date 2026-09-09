import json, tempfile, unittest
from pathlib import Path
from deploy.environments.staging_snapshot import sanitize
class SnapshotTests(unittest.TestCase):
    def test_sanitize_replaces_sensitive_values_and_preserves_keys(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); src=root/'in.jsonl'; out=root/'out.jsonl'
            src.write_text(json.dumps({'record_type':'task','record_key':'1','community_key':'c','address_key':'a','value':'secret','person_name':'real'})+'\n',encoding='utf8')
            self.assertEqual(sanitize(src,out), {'input':1,'output':1,'rejected':0})
            row=json.loads(out.read_text()); self.assertTrue(row['value'].startswith('synthetic-')); self.assertNotIn('person_name',row)
if __name__ == '__main__': unittest.main()
