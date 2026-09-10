import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'backend'))
from deploy.environments.staging_data.codec import Codec, SnapshotError
from deploy.environments.staging_data.relations import history_rows, address_rows


class RelationTests(unittest.TestCase):
    def codec(self):
        codec=Codec(b'a'*32)
        for kind,values in [('actor',[1]),('community',[1]),('property',[1]),('small_community',[1]),('source',[1]),('flow',[1])]:
            codec.allocate(kind,values)
        return codec

    def test_confirmed_address_and_actor_references_survive(self):
        codec=self.codec()
        row={'parser_type':'全链条','row_key':'old','original_address':'虚构原路','suggested_entry_id':1,
            'suggested_community_id':1,'match_status':'confirmed','confirmed_entry_id':1,
            'confirmed_by':1,'confirmed_at':'2026-09-10 00:00:00','manual_unmatched_reason':None,
            'manual_unmatched_address_hmac':None,'manual_unmatched_by':None,'manual_unmatched_at':None}
        remapped={('全链条','old'):{'source':{'row_key':'new'}}}
        rows,_=address_rows([row],remapped,{('全链条','old'):1},codec)
        self.assertEqual(rows[0]['confirmed_entry_id'],codec.reference('small_community',1))
        self.assertEqual(rows[0]['confirmed_by'],codec.reference('actor',1))
        self.assertEqual(rows[0]['match_status'],'confirmed')
        self.assertNotEqual(rows[0]['original_address'],'虚构原路')

    def test_historical_decision_preserves_old_revision_but_no_source_hash(self):
        codec=self.codec()
        events=[{'id':7,'flow_id':1,'stage':'initial_pending','action':'review_decision','outcome':'success',
            'actor_user_id':1,'automatic':0,'source_revision':2,'source_row_hash':'old-hash','created_at':'2026-09-10'}]
        flows={1:{'parser_type':'全链条','row_key':'old'}}
        current={('全链条','old'):{'revision':3,'row_hash':'current-hash'}}
        remapped={('全链条','old'):{'source':{'revision':3,'row_hash':'new-current-hash'}}}
        result=history_rows(events,[],flows,current,remapped,codec)['OnlineData._unverifiable_review_events'][0]
        self.assertEqual(result['source_revision'],2)
        self.assertNotIn(result['source_row_hash'],{'old-hash','current-hash','new-current-hash'})
        self.assertEqual(result['outcome'],'success')
        self.assertIsNone(result['protected_text'])


if __name__=='__main__':unittest.main()
