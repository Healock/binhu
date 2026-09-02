import os
import time
import unittest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.platform_performance import (
    PlatformPerformanceMetrics,
    RequestSample,
    _aggregate,
    _safe_route,
    endpoint_group,
)


class PlatformPerformanceTests(unittest.TestCase):
    def test_unknown_paths_are_sanitized_before_aggregation(self):
        self.assertEqual(
            _safe_route("/api/query/items/123456/detail"),
            "/api/query/items/{id}/detail",
        )
        self.assertEqual(
            _safe_route("/api/jobs/550e8400-e29b-41d4-a716-446655440000"),
            "/api/jobs/{id}",
        )

    def test_endpoint_groups_cover_critical_user_flows(self):
        self.assertEqual(endpoint_group("POST", "/api/auth/login"), "login")
        self.assertEqual(endpoint_group("PATCH", "/api/query/task/{id}"), "task_save")
        self.assertEqual(endpoint_group("GET", "/api/query/task/{id}"), "task_list")
        self.assertEqual(endpoint_group("POST", "/api/tasks/bulk-assign"), "bulk_assignment")
        self.assertEqual(endpoint_group("POST", "/api/address-match/run"), "address_matching")

    def test_409_conflicts_are_not_counted_as_server_errors(self):
        now = time.time()
        samples = [
            RequestSample(now, "PATCH", "/api/query/task/{id}", "task_save", 80, 200, 1),
            RequestSample(now, "PATCH", "/api/query/task/{id}", "task_save", 90, 409, 2),
            RequestSample(now, "GET", "/api/query", "task_list", 120, 503, 1),
        ]
        summary = _aggregate(samples)
        self.assertEqual(summary["conflicts_409"], 1)
        self.assertEqual(summary["errors_5xx"], 1)
        self.assertEqual(summary["error_rate"], 33.33)

    def test_congestion_state_explains_actionable_causes(self):
        metrics = PlatformPerformanceMetrics()
        state, signals = metrics.resolve_state(
            {
                "requests": 100,
                "p95_ms": 3500,
                "error_rate": 0,
            },
            loop_lag_ms=50,
            pool_pressure=1.0,
            mysql_threads_running=5,
            mysql_lock_waits=0,
            background={"oldest_active_seconds": 0, "queued_count": 0},
        )
        self.assertEqual(state, "congested")
        self.assertEqual({item["code"] for item in signals}, {"latency", "db_pool"})
        self.assertTrue(all(item["recommended_action"] for item in signals))
        self.assertTrue(all(item["action_tab"] for item in signals))


if __name__ == "__main__":
    unittest.main()
