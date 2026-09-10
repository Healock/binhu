import unittest
from deploy.environments.staging_data.codec import Codec, SnapshotError, enum, date_value


class SnapshotCodecTests(unittest.TestCase):
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


if __name__=='__main__':unittest.main()
