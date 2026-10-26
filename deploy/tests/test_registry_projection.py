import unittest
from deploy.environments.event_pipeline.services.registry_projection import project_events


def event(event_id, fields):
    return {"schema_version": 1, "event_id": event_id, "event_type": "task.saved", "task_id": "t_fullchain:7",
            "source_id": 7, "revision": 4, "operation_id": event_id, "changed_fields": fields,
            "timestamp": "2026-10-26T00:00:00Z", "environment": "development", "run_id": "dev-registry-1"}


class RegistryProjectionTests(unittest.TestCase):
    def test_registry_projection_keeps_only_revision_watermarks(self):
        row = next(iter(project_events([event("00000000-0000-0000-0000-000000000001", ["community", "person_tags"]) ]).values()))
        self.assertEqual(row["registry_revision"], 4)
        self.assertEqual(row["tag_revision"], 4)
        self.assertNotIn("name", row)


if __name__ == "__main__": unittest.main()
