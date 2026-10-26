import unittest
from deploy.environments.event_pipeline.services.photo_projection import project_events


def event(event_id, kind):
    return {"schema_version": 1, "event_id": event_id, "event_type": kind, "task_id": "t_fullchain:10", "source_id": 10,
            "revision": 1, "operation_id": event_id, "changed_fields": ["photo_attachment"],
            "timestamp": "2026-10-26T00:00:00Z", "environment": "development", "run_id": "dev-photo-1"}


class PhotoProjectionTests(unittest.TestCase):
    def test_photo_metadata_events_are_counted_without_binary_payload(self):
        row = next(iter(project_events([event("00000000-0000-0000-0000-000000000001", "photo.requested"),
                                        event("00000000-0000-0000-0000-000000000002", "photo.attached")]).values()))
        self.assertEqual((row["requested"], row["attached"]), (1, 1))
        self.assertNotIn("content", row)


if __name__ == "__main__": unittest.main()
