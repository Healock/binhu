import copy
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from deploy.environments.staging_data.codec import Codec, SnapshotError
from deploy.environments.staging_data.registry import transform


def fixture():
    return {
        'PlatformData._areas':[{'id':1,'name':'虚构原片区'}],
        'PlatformData._communities':[{'id':1,'name':'虚构原社区','area_id':1,'is_active':1}],
        'RegistryData._police_address_entries':[{'id':1,'name':'虚构原小区','detail_address':'虚构原路一号',
            'community_id':1,'address_type':'community','enabled':1}],
        'RegistryData.registry_properties':[{'id':1,'street':'虚构原街道','community_id':1,
            'natural_address':'虚构原路一号101室','building':'原一栋','room':'原101',
            'housing_type':'个人出租','residence_type':'虚构原用途','source_house_no':'fictional-house-no',
            'status':'active','current_version':3}],
        'RegistryData.registry_property_small_community_links':[{'property_id':1,'small_community_id':1,
            'community_id':1,'match_status':'confirmed','confirmed_by':12,'confirmed_at':'2026-09-10 00:00:00','property_version':3}],
    }


class RegistrySnapshotTests(unittest.TestCase):
    def test_explicit_orphan_exclusion_retains_houses_and_reports_rejections(self):
        rows=fixture()
        links=rows['RegistryData.registry_property_small_community_links']
        links[0].update(small_community_id=99,match_status='suggested',confirmed_by=None,confirmed_at=None)
        original=copy.deepcopy(rows)
        with self.assertRaisesRegex(SnapshotError,'unresolved_property_small_community'):
            transform(rows,Codec(b'a'*32))
        result=transform(rows,Codec(b'a'*32),exclude_orphan_property_links=True)
        self.assertEqual(rows,original)
        self.assertEqual(len(result['tables']['RegistryData.registry_properties']),1)
        self.assertEqual(result['tables']['RegistryData.registry_property_small_community_links'],[])
        self.assertEqual(result['report']['rejected_property_link_count'],1)
        rejected=result['report']['rejected_property_links'][0]
        self.assertEqual(rejected['property_id'],result['tables']['RegistryData.registry_properties'][0]['id'])
        self.assertEqual(rejected['reason'],'missing_small_community')
        for field,value in (('match_status','confirmed'),('confirmed_by',12),('confirmed_at','2026-09-10 00:00:00')):
            protected=copy.deepcopy(rows)
            protected['RegistryData.registry_property_small_community_links'][0][field]=value
            with self.assertRaises(SnapshotError):
                transform(protected,Codec(b'a'*32),exclude_orphan_property_links=True)

    def test_orphan_exclusion_cannot_expand_beyond_three_relations(self):
        rows=fixture()
        prop=rows['RegistryData.registry_properties'][0]
        link=rows['RegistryData.registry_property_small_community_links'][0]
        rows['RegistryData.registry_properties']=[dict(prop,id=i) for i in range(1,5)]
        rows['RegistryData.registry_property_small_community_links']=[dict(link,property_id=i,
            small_community_id=99,match_status='conflict',confirmed_by=None,confirmed_at=None) for i in range(1,5)]
        with self.assertRaisesRegex(SnapshotError,'orphan_exclusion_scope_exceeded'):
            transform(rows,Codec(b'a'*32),exclude_orphan_property_links=True)

    def test_address_types_follow_current_business_contract(self):
        import ast
        tree=ast.parse((Path(__file__).resolve().parents[2]/'backend/routers/police_dispatch.py').read_text(encoding='utf-8'))
        model=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='AddressCreate')
        field=next(node for node in model.body if isinstance(node,ast.AnnAssign) and node.target.id=='address_type')
        allowed=ast.literal_eval(field.annotation.slice)
        for kind in allowed:
            with self.subTest(kind=kind):
                rows=fixture()
                rows['RegistryData._police_address_entries'][0]['address_type']=kind
                result=transform(rows,Codec(b'a'*32))
                self.assertEqual(result['tables']['RegistryData._police_address_entries'][0]['address_type'],kind)
        rows=fixture()
        rows['RegistryData._police_address_entries'][0]['address_type']='unreviewed_text'
        with self.assertRaisesRegex(SnapshotError,'unknown_enum'):
            transform(rows,Codec(b'a'*32))

    def test_relations_and_confirmation_are_preserved_without_source_ids(self):
        result=transform(fixture(),Codec(b'a'*32))
        tables=result['tables']
        prop=tables['RegistryData.registry_properties'][0]
        entry=tables['RegistryData._police_address_entries'][0]
        community=tables['PlatformData._communities'][0]
        link=tables['RegistryData.registry_property_small_community_links'][0]
        self.assertEqual(prop['community_id'],community['id'])
        self.assertEqual(link['property_id'],prop['id'])
        self.assertEqual(link['small_community_id'],entry['id'])
        self.assertEqual(link['confirmed_by'],result['actors'][0]['id'])
        self.assertEqual(link['match_status'],'confirmed')
        self.assertEqual(prop['current_version'],3)
        self.assertNotEqual(prop['id'],1)
        self.assertEqual(result['report']['source_counts'],result['report']['output_counts'])
        self.assertFalse(result['report']['ready_for_application_switch'])

    def test_unknown_column_and_broken_reference_are_rejected(self):
        rows=fixture()
        rows['RegistryData.registry_properties'][0]['private_json']='not exported'
        with self.assertRaises(SnapshotError):transform(rows,Codec(b'a'*32))
        rows=fixture()
        rows['RegistryData.registry_property_small_community_links'][0]['small_community_id']=99
        with self.assertRaises(SnapshotError):transform(rows,Codec(b'a'*32))

    def test_duplicate_primary_key_fails_instead_of_dropping_a_row(self):
        rows=fixture()
        rows['RegistryData.registry_properties'].append(copy.deepcopy(rows['RegistryData.registry_properties'][0]))
        with self.assertRaises(SnapshotError):transform(rows,Codec(b'a'*32))


if __name__=='__main__':unittest.main()
