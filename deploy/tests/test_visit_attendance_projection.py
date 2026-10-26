import unittest
from deploy.environments.event_pipeline.services.visit_attendance_projection import project_events


def event(event_id, kind, fields):
    return {"schema_version": 1, "event_id": event_id, "event_type": kind, "task_id": "t_fullchain:6", "source_id": 6,
            "revision": 2, "operation_id": event_id, "changed_fields": fields, "timestamp": "2026-10-26T00:00:00Z",
            "environment": "development", "run_id": "dev-attendance-1"}


class VisitAttendanceTests(unittest.TestCase):
    def test_attendance_and_review_counters(self):
        row = project_events([event("00000000-0000-0000-0000-000000000001", "task.saved", ["inspector"]),
                              event("00000000-0000-0000-0000-000000000002", "task.reviewed", ["check_result"])])["dev-attendance-1"]
        self.assertEqual(row["attendance_events"], 1)
        self.assertEqual(row["review_events"], 1)


if __name__ == "__main__": unittest.main()
