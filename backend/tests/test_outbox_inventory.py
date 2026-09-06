import pytest
from services.outbox_inventory import SOURCES,source,migration_ready
def test_inventory_contains_all_known_sources():
 assert {x.table for x in SOURCES}=={"_domain_event_outbox","photo_sheet_outbox","_venue_cloud_outbox","_online_projection_jobs"}
 assert all(not x.body_allowed for x in SOURCES)
def test_domain_is_currently_ready_but_external_writebacks_are_pending():
 assert migration_ready("_domain_event_outbox") and not migration_ready("photo_sheet_outbox") and not migration_ready("_venue_cloud_outbox") and not migration_ready("_online_projection_jobs")
@pytest.mark.parametrize("table",["unknown","domain_events","_online_source_rows"])
def test_unknown_source_rejected(table):
 with pytest.raises(ValueError):source(table)
