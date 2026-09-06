"""Venue business writes must atomically record metadata without cloud calls."""
import json
import os
from unittest.mock import AsyncMock

import pytest
os.environ.setdefault('MYSQL_PASSWORD', 'fixture-password')
os.environ.setdefault('ENCRYPTION_KEY', 'fixture-key')
from config import settings, Settings
from routers import venue_codes
from services import venue_cloud


class Cursor:
    lastrowid = 41

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.delivery = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, args=None):
        self.calls.append((sql, args))
        if 'INSERT INTO _kafka_event_delivery' in sql:
            if self.fail:
                raise RuntimeError('fixture ledger failure')
            self.delivery = args

    async def fetchone(self):
        if self.calls[-1][0].startswith('SELECT config_revision,token_version'):
            return 4, 2, None
        return self.delivery[1], self.delivery[3]


@pytest.fixture
def shadow(monkeypatch):
    for key, value in {
        'KAFKA_AUX_EVENTS_ENABLED': True, 'APP_ENVIRONMENT': 'shadow',
        'LOAD_TEST_RUN_ID': 'KSHADOW-venue-fixture',
        'MYSQL_ONLINE_DATA_DB': 'KShadow_venue_fixture',
        'MYSQL_REGISTRY_DB': 'KShadow_venue_fixture',
        'VENUE_CLOUD_SYNC_ENABLED': False, 'VENUE_CLOUD_PULL_ENABLED': False,
    }.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(venue_cloud, 'VenueCloudClient', lambda *a, **k: pytest.fail('external client created'))


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['create', 'update', 'disable', 'delete', 'rotate'])
async def test_venue_source_and_delivery_use_same_cursor_without_cloud(shadow, action):
    cur = Cursor()
    request_id = await venue_cloud.enqueue_venue_cloud_outbox(cur, 41, 5, action)
    assert request_id
    assert cur.calls[0][0].startswith('INSERT INTO _venue_cloud_outbox')
    event = json.loads(cur.delivery[2])
    assert event['event_id'] == request_id
    assert (event['venue_id'], event['config_revision'], event['action']) == (41, 5, action)
    assert event['event_type'] == 'venue.sync.requested'
    assert set(event) == {'schema_version','event_type','event_id','venue_id','config_revision','action','timestamp','environment','run_id'}
    assert not any('UPDATE _venue_cloud_outbox' in sql for sql, _ in cur.calls)


@pytest.mark.asyncio
async def test_disabled_feature_does_not_create_outbox(monkeypatch):
    monkeypatch.setattr(settings, 'KAFKA_AUX_EVENTS_ENABLED', False)
    monkeypatch.setattr(settings, 'VENUE_CLOUD_SYNC_ENABLED', False)
    cur = Cursor()
    assert await venue_cloud.enqueue_venue_cloud_outbox(cur, 41, 5, 'create') is None
    assert cur.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('key,value', [('APP_ENVIRONMENT','production'), ('LOAD_TEST_RUN_ID','LT-old'), ('MYSQL_REGISTRY_DB','RegistryData'), ('VENUE_CLOUD_SYNC_ENABLED',True)])
async def test_invalid_scope_fails_before_source_insert(shadow, monkeypatch, key, value):
    monkeypatch.setattr(settings, key, value)
    cur = Cursor()
    with pytest.raises(ValueError):
        await venue_cloud.enqueue_venue_cloud_outbox(cur, 41, 5, 'create')
    assert cur.calls == []


@pytest.mark.asyncio
async def test_actual_create_route_rolls_back_source_and_delivery(shadow, monkeypatch):
    from types import SimpleNamespace
    cur = Cursor(fail=True)
    conn = SimpleNamespace(cursor=lambda:cur, begin=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(venue_codes, '_public_venue_url', lambda token:'fixture-url')
    monkeypatch.setattr(venue_codes, '_token_digest', lambda token:'fixture-hmac')
    monkeypatch.setattr(venue_codes, 'encrypt_secret', lambda token:'fixture-encrypted')
    audit = AsyncMock()
    monkeypatch.setattr(venue_codes, 'record_admin_audit', audit)
    data = SimpleNamespace(name='fixture', venue_type='fixture', address='fixture', community_id=1, community_name='fixture', status='active')
    with pytest.raises(RuntimeError, match='fixture ledger failure'):
        await venue_codes.create_venue(data, None, {'id':1}, conn)
    conn.rollback.assert_awaited_once()
    conn.commit.assert_not_awaited()
    audit.assert_not_awaited()
    assert '_venue_codes' in cur.calls[0][0]
    assert '_venue_cloud_outbox' in cur.calls[1][0]


def test_aux_flag_cannot_start_in_production():
    with pytest.raises(ValueError):
        Settings(_env_file=None, KAFKA_AUX_EVENTS_ENABLED=True, APP_ENVIRONMENT='production', MYSQL_PASSWORD='fixture', ENCRYPTION_KEY='fixture')


def test_aux_startup_accepts_only_isolated_colocated_databases():
    config = dict(KAFKA_AUX_EVENTS_ENABLED=True, APP_ENVIRONMENT='shadow',
        LOAD_TEST_RUN_ID='KSHADOW-venue-fixture', SESSION_COOKIE_NAME='binhu_shadow_session',
        MYSQL_PASSWORD='fixture', ENCRYPTION_KEY='fixture',
        MYSQL_ONLINE_DATA_DB='KShadow_fixture', MYSQL_REGISTRY_DB='KShadow_fixture',
        VENUE_CLOUD_SYNC_ENABLED=False, VENUE_CLOUD_PULL_ENABLED=False)
    assert Settings(_env_file=None, **config).KAFKA_AUX_EVENTS_ENABLED
    for change in ({'MYSQL_REGISTRY_DB':'RegistryData'}, {'VENUE_CLOUD_PULL_ENABLED':True}, {'LOAD_TEST_RUN_ID':'LT-old'}):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **{**config, **change})


@pytest.mark.asyncio
async def test_external_only_mode_keeps_existing_source_behavior(monkeypatch):
    monkeypatch.setattr(settings, 'KAFKA_AUX_EVENTS_ENABLED', False)
    monkeypatch.setattr(settings, 'VENUE_CLOUD_SYNC_ENABLED', True)
    cur = Cursor()
    assert await venue_cloud.enqueue_venue_cloud_outbox(cur, 41, 5, 'create')
    assert len(cur.calls) == 1 and cur.delivery is None


@pytest.mark.asyncio
async def test_invalid_metadata_does_not_insert_source(shadow):
    cur = Cursor()
    with pytest.raises(ValueError):
        await venue_cloud.enqueue_venue_cloud_outbox(cur, 41, 0, 'create')
    assert cur.calls == []


@pytest.mark.asyncio
async def test_shadow_worker_never_claims_or_calls_cloud(shadow, monkeypatch):
    claim = AsyncMock(side_effect=AssertionError('external source claim'))
    monkeypatch.setattr(venue_cloud, '_claim_outbox_rows', claim)
    client = AsyncMock()
    assert await venue_cloud.process_outbox_once(client) == 0
    claim.assert_not_awaited()
    client.request_json.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('fail', [False, True])
async def test_actual_local_rotate_route_records_event_or_rolls_back(shadow, monkeypatch, fail):
    from types import SimpleNamespace
    cur = Cursor(fail=fail)
    conn = SimpleNamespace(cursor=lambda:cur, begin=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(venue_codes, '_public_venue_url', lambda token:'fixture-url')
    monkeypatch.setattr(venue_codes, '_token_digest', lambda token:'fixture-hmac')
    monkeypatch.setattr(venue_codes, 'encrypt_secret', lambda token:'fixture-encrypted')
    monkeypatch.setattr(venue_codes, 'record_admin_audit', AsyncMock())
    monkeypatch.setattr(venue_codes, 'request_audit_fields', lambda request:{})
    if fail:
        with pytest.raises(RuntimeError, match='fixture ledger failure'):
            await venue_codes.rotate_token(41, None, {'id':1}, conn)
        conn.commit.assert_not_awaited()
        conn.rollback.assert_awaited_once()
    else:
        result = await venue_codes.rotate_token(41, None, {'id':1}, conn)
        assert result['cloud_sync_status'] == 'local_only'
        conn.commit.assert_awaited_once()
        event = json.loads(cur.delivery[2])
        assert event['action'] == 'rotate' and event['config_revision'] == 5
        assert 'token' not in event and 'encrypted_token' not in event
    assert 'token_version' in cur.calls[1][0]
    assert 'INSERT INTO _venue_cloud_outbox' in cur.calls[2][0]
