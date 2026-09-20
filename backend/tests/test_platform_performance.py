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
        self.assertEqual(endpoint_group("POST", "/api/presence/heartbeat"), "polling")
        self.assertEqual(endpoint_group("GET", "/api/query/全链条/version"), "polling")
        self.assertEqual(endpoint_group("GET", "/api/events/stream"), "realtime")

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

    def test_realtime_metrics_track_connections_reconnect_peak_and_resync(self):
        metrics = PlatformPerformanceMetrics()
        metrics.realtime_open("sse")
        metrics.realtime_open("sse", reconnect=True)
        metrics.realtime_open("websocket")
        metrics.realtime_resync()
        metrics.realtime_close("sse")
        snapshot = metrics.realtime_snapshot(15)
        self.assertEqual(snapshot["current_connections"], {"sse": 1, "websocket": 1})
        self.assertEqual(snapshot["opened_since_start"], {"sse": 2, "websocket": 1})
        self.assertEqual(snapshot["reconnects"], 1)
        self.assertEqual(snapshot["reconnect_peak_per_minute"], 1)
        self.assertEqual(snapshot["resync_required_since_start"], 1)


if __name__ == "__main__":
    unittest.main()
