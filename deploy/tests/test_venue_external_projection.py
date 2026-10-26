import unittest
from deploy.environments.event_pipeline.services.venue_external_projection import project_events
from deploy.environments.event_pipeline.services.kafka_event_contract import EventContractError


def event(event_id, kind):
    field = "venue_visit" if kind.startswith("venue") else "external_stat"
    return {"schema_version": 1, "event_id": event_id, "event_type": kind, "task_id": "t_fullchain:11", "source_id": 11,
            "revision": 1, "operation_id": event_id, "changed_fields": [field], "timestamp": "2026-10-26T00:00:00Z",
            "environment": "development", "run_id": "dev-venue-1"}


class VenueExternalProjectionTests(unittest.TestCase):
    def test_venue_and_external_events_are_separate(self):
        row = project_events([event("00000000-0000-0000-0000-000000000001", "venue.visited"),
                              event("00000000-0000-0000-0000-000000000002", "external.stats.updated")])["dev-venue-1"]
        self.assertEqual((row["venue_visits"], row["external_updates"]), (1, 1))

    def test_reused_event_id_with_different_metadata_is_rejected(self):
        first = event("00000000-0000-0000-0000-000000000001", "venue.visited")
        second = {**first, "changed_fields": ["external_stat"]}
        with self.assertRaisesRegex(EventContractError, "reused"):
            project_events([first, second])


if __name__ == "__main__": unittest.main()
