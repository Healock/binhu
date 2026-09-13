import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.environments.event_pipeline.dual_track import compare
from deploy.environments.event_pipeline.services.task_metadata_projection import (
    IncrementalTaskMetadataProjector, ProjectionConflict, flatten_projection, project_events,
)
from deploy.environments.event_pipeline.services.python_metadata_worker import upsert_sql


def event(event_id, revision, event_type="task.saved", fields=None):
    return {
        "schema_version": 1, "event_id": event_id,
        "operation_id": "22222222-2222-4222-8222-222222222222",
        "event_type": event_type, "task_id": "t_fullchain:1", "source_id": 1,
        "revision": revision, "changed_fields": fields or ["task_state"],
        "timestamp": "2026-09-10T00:00:00Z", "environment": "development",
        "run_id": "dev-test-1",
    }


class TaskMetadataProjectionTests(unittest.TestCase):
    def test_projection_is_deduplicated_and_counts_metadata(self):
        rows = project_events([
            event("11111111-1111-4111-8111-111111111111", 1, "task.created", ["task_state"]),
            event("33333333-3333-4333-8333-333333333333", 3, "task.reviewed", ["task_state", "check_result"]),
            event("22222222-2222-4222-8222-222222222222", 2, "task.saved", ["address"]),
            event("22222222-2222-4222-8222-222222222222", 2, "task.saved", ["address"]),
        ])
        result = flatten_projection(rows[("dev-test-1", "t_fullchain:1", 1)])
        self.assertEqual(result["revision"], 3)
        self.assertEqual(result["event_count"], 3)
        self.assertEqual(result["changed_field_count"], 4)
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["saved_count"], 1)
        self.assertEqual(result["reviewed_count"], 1)

    def test_conflicting_event_id_or_revision_is_hard_failure(self):
        with self.assertRaises(ProjectionConflict):
            project_events([
                event("11111111-1111-4111-8111-111111111111", 1),
                {**event("11111111-1111-4111-8111-111111111111", 1), "event_type": "task.deleted"},
            ])

    def test_python_worker_writes_only_the_isolated_projection_table(self):
        row = next(iter(project_events([
            event("11111111-1111-4111-8111-111111111111", 1),
        ]).values()))
        sql, params = upsert_sql(row)
        self.assertIn("INSERT INTO dev_task_metadata_python", sql)
        self.assertNotIn("OnlineData", sql)
        self.assertEqual(params[0], "dev-test-1")
        self.assertEqual(params[3], 1)
        with self.assertRaises(ProjectionConflict):
            project_events([
                event("11111111-1111-4111-8111-111111111111", 1),
                event("33333333-3333-4333-8333-333333333333", 1, "task.deleted"),
            ])

    def test_incremental_projector_deduplicates_and_bounds_event_cache(self):
        projector = IncrementalTaskMetadataProjector(max_event_ids=2)
        projector.apply(event("11111111-1111-4111-8111-111111111111", 1))
        projector.apply(event("22222222-2222-4222-8222-222222222222", 2))
        projector.apply(event("33333333-3333-4333-8333-333333333333", 3))
        self.assertEqual(projector.event_cache_size, 2)
        self.assertEqual(
            flatten_projection(projector.snapshot(("dev-test-1", "t_fullchain:1", 1)))["event_count"],
            3,
        )
        with self.assertRaises(ProjectionConflict):
            projector.apply({
                **event("33333333-3333-4333-8333-333333333333", 3),
                "event_type": "task.deleted",
            })

    def test_dual_track_report_is_redacted_and_records_mismatch(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            py = root / "python.jsonl"
            fl = root / "flink.jsonl"
            out = root / "report.json"
            py.write_text(json.dumps(event("11111111-1111-4111-8111-111111111111", 1, "task.saved")) + "\n", encoding="utf-8")
            fl.write_text(json.dumps(event("11111111-1111-4111-8111-111111111111", 2, "task.saved")) + "\n", encoding="utf-8")
            with patch.dict("os.environ", {"APP_ENVIRONMENT": "development"}):
                report = compare(py, fl, "dev-test-1", "dual-track-20260914-mismatch", out)
            self.assertFalse(report["passed"])
            self.assertEqual(report["unattributed_difference_count"], 1)
            payload = out.read_text(encoding="utf-8")
            self.assertNotIn("姓名", payload)
            self.assertNotIn("手机号", payload)
            self.assertIn("task_id_sha256", payload)


if __name__ == "__main__":
    unittest.main()
