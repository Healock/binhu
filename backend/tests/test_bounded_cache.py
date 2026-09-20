import time
import unittest

from services.bounded_cache import BoundedTTLCache


class BoundedCacheTests(unittest.TestCase):
    def test_ttl_capacity_and_invalidation(self):
        cache = BoundedTTLCache[str, int](ttl_seconds=.02, max_entries=2)
        cache.set("a", 1); cache.set("b", 2); cache.set("c", 3)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), 2)
        cache.invalidate("b")
        self.assertIsNone(cache.get("b"))
        time.sleep(.03)
        self.assertIsNone(cache.get("c"))


if __name__ == "__main__":
    unittest.main()
