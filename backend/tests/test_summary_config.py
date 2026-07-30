import os
import unittest

from fastapi import HTTPException

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from deps import require_admin
from routers.stats import _normalize_summary_types, router as stats_router


class SummaryConfigTests(unittest.TestCase):
    def test_summary_config_routes_require_admin(self):
        routes = [
            route
            for route in stats_router.routes
            if route.path == "/api/stats/summary-config"
        ]
        self.assertEqual(len(routes), 2)
        for route in routes:
            self.assertTrue(any(
                dependency.call is require_admin
                for dependency in route.dependant.dependencies
            ))

    def test_summary_types_are_deduplicated_and_validated(self):
        self.assertEqual(
            _normalize_summary_types(["全链条", "全链条", "寄递业"]),
            ["全链条", "寄递业"],
        )
        with self.assertRaises(HTTPException) as raised:
            _normalize_summary_types(["不存在的业务"])
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
