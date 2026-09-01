from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from config import settings
from services.venue_cloud_security import (
    VenueCloudSecurityError,
    canonical_json,
    load_request_signing_key,
    load_response_verify_key,
    sign_request,
    verify_response,
)


class VenueCloudClientError(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def validate_wait_response(value: dict[str, Any]) -> dict[str, Any]:
    available = value.get("available")
    pending_count = value.get("pending_count")
    wake_reason = value.get("wake_reason")
    if (
        type(available) is not bool
        or type(pending_count) is not int
        or pending_count < 0
        or wake_reason not in {"available", "timeout"}
    ):
        raise VenueCloudClientError("invalid_cloud_response")
    if available != (pending_count > 0) or (available and wake_reason != "available") or (
        not available and wake_reason != "timeout"
    ):
        raise VenueCloudClientError("invalid_cloud_response")
    return {
        "available": available,
        "pending_count": pending_count,
        "wake_reason": wake_reason,
    }


def validate_status_response(value: dict[str, Any]) -> dict[str, Any]:
    pending_count = value.get("pending_count")
    uncertain_count = value.get("uncertain_count")
    active_venue_count = value.get("active_venue_count")
    active_key_id = value.get("active_key_id")
    if (
        value.get("status") != "ok"
        or type(pending_count) is not int
        or pending_count < 0
        or type(uncertain_count) is not int
        or uncertain_count < 0
        or type(active_venue_count) is not int
        or active_venue_count < 0
        or not isinstance(active_key_id, str)
        or not active_key_id
    ):
        raise VenueCloudClientError("invalid_cloud_response")
    return value


def validate_venue_cloud_configuration() -> str:
    required = {
        "client_certificate_missing": settings.VENUE_CLOUD_CLIENT_CERT_PATH,
        "client_key_missing": settings.VENUE_CLOUD_CLIENT_KEY_PATH,
        "request_signing_key_missing": settings.VENUE_CLOUD_REQUEST_SIGNING_KEY_PATH,
        "response_verify_key_missing": settings.VENUE_CLOUD_RESPONSE_SIGNING_PUBLIC_KEY_PATH,
    }
    for reason, path in required.items():
        if not path or not Path(path).is_file():
            raise VenueCloudClientError(reason)
    decryption_dir = Path(settings.VENUE_CLOUD_DECRYPTION_KEY_DIR)
    if not settings.VENUE_CLOUD_DECRYPTION_KEY_DIR or not decryption_dir.is_dir():
        raise VenueCloudClientError("decryption_key_directory_missing")
    base_url = settings.VENUE_CLOUD_BASE_URL.rstrip("/")
    try:
        parsed = urlsplit(base_url)
        valid_port = parsed.port in (None, 443)
    except ValueError as exc:
        raise VenueCloudClientError("valid_cloud_endpoint_required") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not valid_port
        or parsed.path not in ("", "/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise VenueCloudClientError("valid_cloud_endpoint_required")
    return base_url


class VenueCloudClient:
    def __init__(self):
        self.base_url = validate_venue_cloud_configuration()
        self.request_key = load_request_signing_key(settings.VENUE_CLOUD_REQUEST_SIGNING_KEY_PATH)
        self.response_key = load_response_verify_key(settings.VENUE_CLOUD_RESPONSE_SIGNING_PUBLIC_KEY_PATH)
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            cert=(settings.VENUE_CLOUD_CLIENT_CERT_PATH, settings.VENUE_CLOUD_CLIENT_KEY_PATH),
            timeout=settings.VENUE_CLOUD_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self, method: str, path: str, request_id: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        return {
            "Content-Type": "application/json",
            "X-Binhu-Timestamp": timestamp,
            "X-Binhu-Nonce": nonce,
            "X-Binhu-Request-Id": request_id,
            "X-Binhu-Signature": sign_request(
                self.request_key,
                method=method,
                path=path,
                timestamp=timestamp,
                nonce=nonce,
                request_id=request_id,
                body=body,
            ),
        }

    async def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = str((payload or {}).get("request_id") or uuid.uuid4())
        body = canonical_json(payload) if payload is not None else b""
        try:
            response = await self.client.request(method, path, content=body or None, headers=self._headers(method, path, request_id, body))
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise VenueCloudClientError("transport_error") from exc
        if response.status_code >= 400:
            if response.status_code in {401, 403}:
                reason = "authentication_failed"
            elif response.status_code == 409:
                reason = "request_conflict"
            elif response.status_code >= 500:
                reason = "cloud_unavailable"
            else:
                reason = "cloud_request_rejected"
            raise VenueCloudClientError(reason)
        timestamp = response.headers.get("X-Binhu-Response-Timestamp", "")
        signature = response.headers.get("X-Binhu-Response-Signature", "")
        try:
            verify_response(
                self.response_key,
                request_id=request_id,
                timestamp=timestamp,
                signature=signature,
                body=response.content,
            )
            value = response.json()
        except (VenueCloudSecurityError, ValueError) as exc:
            raise VenueCloudClientError("invalid_cloud_response") from exc
        if not isinstance(value, dict):
            raise VenueCloudClientError("invalid_cloud_response")
        return value

    async def wait_for_submissions(self, worker_id: str, timeout_seconds: int = 20) -> dict[str, Any]:
        bounded_timeout = min(20, max(1, int(timeout_seconds)))
        return validate_wait_response(
            await self.request_json(
                "POST",
                "/api/internal/submissions/wait",
                {
                    "request_id": str(uuid.uuid4()),
                    "worker_id": worker_id,
                    "timeout_seconds": bounded_timeout,
                },
            )
        )

    async def download_photo(self, path: str, request_id: str, expected_size: int, expected_sha256: str) -> bytes:
        if not path.startswith("/api/internal/submissions/") or ".." in path:
            raise VenueCloudClientError("invalid_photo_path")
        try:
            response = await self.client.get(path, headers=self._headers("GET", path, request_id, b""))
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise VenueCloudClientError("transport_error") from exc
        if response.status_code != 200:
            raise VenueCloudClientError("photo_download_failed")
        data = response.content
        if len(data) != expected_size:
            raise VenueCloudClientError("photo_length_mismatch")
        import hashlib

        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise VenueCloudClientError("photo_hash_mismatch")
        return data
