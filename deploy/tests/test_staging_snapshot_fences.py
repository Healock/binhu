import json
import unittest
from deploy.environments.staging_data.codec import Codec, SnapshotError
from deploy.environments.staging_data.fences import transform_fence


class FenceTests(unittest.TestCase):
    def test_equal_and_stale_hashes_preserve_equality_semantics(self):
        codec = Codec(b'a' * 32)
        source = {'revision': 9, 'row_hash': 'a' * 64}
        safe = {'revision': 9, 'row_hash': 'b' * 64}
        for revision, original_hash in [(9, 'a' * 64), (7, 'a' * 64),
                                         (9, 'c' * 64), (0, ''), (None, '')]:
            with self.subTest(revision=revision, hash=original_hash[:1]):
                result = transform_fence({'source_revision': revision,
                    'source_row_hash': original_hash}, source, safe, codec)
                self.assertEqual(result['source_revision'], revision)
                self.assertEqual(result['source_row_hash'] == safe['row_hash'],
                                 original_hash == source['row_hash'])
                self.assertEqual(bool(result['source_row_hash']), bool(original_hash))
                if original_hash:
                    self.assertNotEqual(result['source_row_hash'], original_hash)

    def test_embedded_json_is_scanned_even_when_unicode_escaped(self):
        codec = Codec(b'a' * 32)
        codec.remember('虚构敏感正文')
        self.assertEqual(codec.scan({'values_json': json.dumps(
            {'nested': ['虚构敏感正文']}, ensure_ascii=True)}), 1)
        self.assertEqual(codec.scan({'values_json': '{"text":"safe"}'}), 0)
        with self.assertRaises(SnapshotError):
            codec.scan({'values_json': '{invalid'})


if __name__ == '__main__':
    unittest.main()
