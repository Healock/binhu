"""Shadow-only metadata transport; never authorizes external cloud effects."""
from __future__ import annotations

import re


def auxiliary_bridge_config(settings) -> dict[str, str] | None:
    if not getattr(settings, 'KAFKA_AUX_EVENTS_ENABLED', False):
        return None
    run_id = str(settings.LOAD_TEST_RUN_ID or '')
    if settings.APP_ENVIRONMENT != 'shadow' or not re.fullmatch(r'KSHADOW-[A-Za-z0-9][A-Za-z0-9_-]{0,63}', run_id):
        raise ValueError('Kafka auxiliary events require a KSHADOW environment')
    # This POC relay reads one prepared ledger. Never take a second database
    # connection or silently write to a different domain's unconsumed ledger.
    database = settings.MYSQL_ONLINE_DATA_DB
    if not re.fullmatch(r'KShadow_[A-Za-z0-9_]{1,50}', database) or settings.MYSQL_REGISTRY_DB != database:
        raise ValueError('Kafka auxiliary events require the isolated colocated registry ledger')
    if settings.VENUE_CLOUD_SYNC_ENABLED or settings.VENUE_CLOUD_PULL_ENABLED:
        raise ValueError('Kafka auxiliary shadow must keep external venue operations disabled')
    return {'run_id': run_id, 'database': database}
