import unittest
from deploy.environments.event_pipeline.services.qmf_projection import project_events


def event(event_id, kind="task.created", fields=None):
    return {"schema_version": 1, "event_id": event_id, "event_type": kind, "task_id": "t_fullchain:5", "source_id": 5,
            "revision": 2, "operation_id": event_id, "changed_fields": fields or ["task_state"],
            "timestamp": "2026-10-26T00:00:00Z", "environment": "development", "run_id": "dev-qmf-1"}


class QmfProjectionTests(unittest.TestCase):
    def test_acquisition_and_registration_are_separate_counters(self):
        row = project_events([event("00000000-0000-0000-0000-000000000001"), event("00000000-0000-0000-0000-000000000002", "task.assigned", ["community"])])["dev-qmf-1"]
        self.assertEqual(row["acquisition_events"], 1)
        self.assertEqual(row["registration_events"], 1)


if __name__ == "__main__": unittest.main()
