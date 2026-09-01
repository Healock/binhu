import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.venue_cloud as venue_cloud
from services.venue_cloud_client import (
    VenueCloudClientError,
    validate_status_response,
    validate_venue_cloud_configuration,
    validate_wait_response,
)


class FakeClient:
    def __init__(self, signal):
        self.signal = signal
        self.wait_calls = []

    async def wait_for_submissions(self, worker_id, timeout_seconds=20):
        self.wait_calls.append((worker_id, timeout_seconds))
        return self.signal


@pytest.mark.asyncio
async def test_worker_wake_drains_until_queue_is_empty(monkeypatch):
    client = FakeClient({"available": True, "pending_count": 2, "wake_reason": "available"})
    batches = iter([2, 1, 0])
    monkeypatch.setattr(venue_cloud.settings, "VENUE_CLOUD_PULL_ENABLED", True)
    monkeypatch.setattr(venue_cloud.settings, "VENUE_CLOUD_WORKER_ID", "binhu-primary")
    monkeypatch.setattr(venue_cloud, "pull_submissions_once", lambda _client: _async_next(batches))

    pulled = await venue_cloud.wait_for_and_drain(client)

    assert pulled == 3
    assert client.wait_calls == [("binhu-primary", 20)]


@pytest.mark.asyncio
async def test_five_minute_fallback_pull_does_not_wait_for_signal(monkeypatch):
    client = FakeClient({"available": False, "pending_count": 0, "wake_reason": "timeout"})
    batches = iter([1, 0])
    monkeypatch.setattr(venue_cloud.settings, "VENUE_CLOUD_PULL_ENABLED", True)
    monkeypatch.setattr(venue_cloud, "pull_submissions_once", lambda _client: _async_next(batches))

    pulled = await venue_cloud.wait_for_and_drain(client, fallback_due=True)

    assert pulled == 1
    assert client.wait_calls == []


@pytest.mark.asyncio
async def test_wait_timeout_returns_without_pull(monkeypatch):
    client = FakeClient({"available": False, "pending_count": 0, "wake_reason": "timeout"})
    calls = []
    monkeypatch.setattr(venue_cloud.settings, "VENUE_CLOUD_PULL_ENABLED", True)

    async def pull(_client):
        calls.append(True)
        return 0

    monkeypatch.setattr(venue_cloud, "pull_submissions_once", pull)

    assert await venue_cloud.wait_for_and_drain(client) == 0
    assert calls == []


@pytest.mark.asyncio
async def test_existing_cloud_submission_is_acknowledged_without_downloading_again(monkeypatch):
    class NoDownloadClient:
        async def download_photo(self, *_args, **_kwargs):
            raise AssertionError("an idempotent replay must not download the photo again")

    monkeypatch.setattr(venue_cloud, "_existing_cloud_visit", lambda _submission_id: _async_value(True))

    result = await venue_cloud._ingest_item(
        NoDownloadClient(),
        "lease-id",
        {"submission_id": "submission-id", "local_venue_id": 7, "key_id": "key-1"},
    )

    assert result == {"submission_id": "submission-id", "status": "accepted", "reason_code": ""}


@pytest.mark.asyncio
async def test_pull_rejects_incomplete_acknowledgement(monkeypatch):
    class IncompleteAckClient:
        def __init__(self):
            self.calls = 0

        async def request_json(self, _method, path, _payload=None):
            self.calls += 1
            if path.endswith("/pull"):
                return {
                    "lease_id": "lease-id",
                    "lease_expires_at": "2099-01-01T00:00:00Z",
                    "items": [{"submission_id": "submission-id"}],
                }
            if path.endswith("/ack"):
                return {"applied": []}
            raise AssertionError(path)

    monkeypatch.setattr(venue_cloud.settings, "VENUE_CLOUD_PULL_ENABLED", True)
    monkeypatch.setattr(
        venue_cloud,
        "_ingest_item",
        lambda *_args: _async_value(
            {"submission_id": "submission-id", "status": "accepted", "reason_code": ""}
        ),
    )

    with pytest.raises(VenueCloudClientError, match="acknowledgement_incomplete"):
        await venue_cloud.pull_submissions_once(IncompleteAckClient())


@pytest.mark.parametrize(
    "response",
    [
        {"available": "false", "pending_count": 0, "wake_reason": "timeout"},
        {"available": False, "pending_count": True, "wake_reason": "timeout"},
        {"available": False, "pending_count": -1, "wake_reason": "timeout"},
        {"available": False, "pending_count": 1, "wake_reason": "timeout"},
        {"available": True, "pending_count": 0, "wake_reason": "available"},
        {"available": True, "pending_count": 1, "wake_reason": "timeout"},
    ],
)
def test_wait_response_rejects_malformed_or_inconsistent_values(response):
    with pytest.raises(VenueCloudClientError, match="invalid_cloud_response"):
        validate_wait_response(response)


def test_wait_response_accepts_only_consistent_states():
    available = {"available": True, "pending_count": 2, "wake_reason": "available"}
    timeout = {"available": False, "pending_count": 0, "wake_reason": "timeout"}

    assert validate_wait_response(available) == available
    assert validate_wait_response(timeout) == timeout


def test_status_response_requires_complete_non_negative_counts_and_key_id():
    valid = {
        "status": "ok",
        "pending_count": 0,
        "uncertain_count": 1,
        "active_venue_count": 2,
        "active_key_id": "venue-rsa-2026-09",
    }
    assert validate_status_response(valid) == valid

    for field, invalid in (
        ("pending_count", "0"),
        ("uncertain_count", -1),
        ("active_venue_count", False),
        ("active_key_id", ""),
        ("status", "ready"),
    ):
        candidate = {**valid, field: invalid}
        with pytest.raises(VenueCloudClientError, match="invalid_cloud_response"):
            validate_status_response(candidate)


def test_enabled_configuration_requires_all_five_secret_locations(monkeypatch, tmp_path):
    monkeypatch.setattr(venue_cloud.settings, "VENUE_CLOUD_BASE_URL", "https://venue-cloud.example.test")
    monkeypatch.setattr(venue_cloud.settings, "VENUE_CLOUD_CLIENT_CERT_PATH", str(tmp_path / "missing-cert"))
    monkeypatch.setattr(venue_cloud.settings, "VENUE_CLOUD_CLIENT_KEY_PATH", str(tmp_path / "missing-key"))
    monkeypatch.setattr(venue_cloud.settings, "VENUE_CLOUD_REQUEST_SIGNING_KEY_PATH", str(tmp_path / "missing-signing"))
    monkeypatch.setattr(venue_cloud.settings, "VENUE_CLOUD_RESPONSE_SIGNING_PUBLIC_KEY_PATH", str(tmp_path / "missing-verify"))
    monkeypatch.setattr(venue_cloud.settings, "VENUE_CLOUD_DECRYPTION_KEY_DIR", str(tmp_path / "missing-decryption"))

    with pytest.raises(VenueCloudClientError, match="client_certificate_missing"):
        validate_venue_cloud_configuration()


async def _async_next(values):
    return next(values)


async def _async_value(value):
    return value
