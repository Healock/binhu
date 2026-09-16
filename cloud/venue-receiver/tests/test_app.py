import io
import sys
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.main import create_app
from app.security import b64encode, canonical_request, keyed_digest


class FakeRepository:
    def __init__(self):
        self.venue = None
        self.form_tokens = set()
        self.public_form = None
        self.public_form_tokens = {}
        self.submissions = {}
        self.nonces = set()

    async def ping(self):
        return None

    async def get_venue_by_token(self, token_hmac):
        if self.venue and self.venue["token_hmac"] == token_hmac:
            return self.venue
        return None

    async def check_rate_limits(self, _limits):
        return True

    async def issue_form_token(self, token_hmac, venue_id, expires_at):
        self.form_tokens.add((token_hmac, venue_id))

    async def consume_form_token(self, token_hmac, venue_id):
        key = (token_hmac, venue_id)
        if key not in self.form_tokens:
            return False
        self.form_tokens.remove(key)
        return True

    async def get_public_form_by_token(self, token_hmac):
        if self.public_form and self.public_form["token_hmac"] == token_hmac:
            return self.public_form
        return None

    async def issue_public_form_token(self, token_hmac, form_key, not_before, expires_at):
        self.public_form_tokens[(token_hmac, form_key)] = (not_before, expires_at)

    async def consume_public_form_token(self, token_hmac, form_key):
        times = self.public_form_tokens.pop((token_hmac, form_key), None)
        if not times:
            return False
        now = datetime.utcnow()
        if not times[0] <= now <= times[1]:
            self.public_form_tokens[(token_hmac, form_key)] = times
            return False
        return True

    async def upsert_public_form(self, item):
        self.public_form = {**item, "token_hmac": item["token_hmac"]}
        return {"applied": True, "config_revision": item["config_revision"], "status": item["status"], "token_version": item["token_version"]}

    async def get_submission(self, submission_id):
        return self.submissions.get(submission_id)

    async def create_submission(self, item):
        self.submissions[item["submission_id"]] = {
            "submission_id": item["submission_id"],
            "local_venue_id": item.get("local_venue_id"),
            "public_form_key": item.get("public_form_key"),
            "request_fingerprint": item["request_fingerprint"],
            "state": "queued",
        }

    async def available_submission_count(self):
        return sum(1 for item in self.submissions.values() if item["state"] == "queued")

    async def claim_nonce(self, nonce, request_id):
        key = (nonce, request_id)
        if key in self.nonces:
            return False
        self.nonces.add(key)
        return True

    async def status(self):
        return {
            "pending_count": await self.available_submission_count(),
            "uncertain_count": 0,
            "oldest_pending_at": None,
            "active_venue_count": 0,
        }

    async def expire_records(self, *_args):
        return []

    async def get_request_result(self, *_args):
        return None

    async def save_request_result(self, *_args):
        return None

    async def upsert_venue(self, item):
        self.venue = {**item, "token_hmac": item["token_hmac"]}
        return {"applied": True, "config_revision": item["config_revision"], "status": item["status"], "token_version": item["token_version"]}


def make_client(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "key-1.pem").write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    config = Settings(
        MYSQL_PASSWORD="test-password",
        PUBLIC_TOKEN_HMAC_KEY="a" * 32,
        FORM_TOKEN_HMAC_KEY="b" * 32,
        REQUEST_FINGERPRINT_KEY="c" * 32,
        ACTIVE_ENCRYPTION_KEY_ID="key-1",
        ENCRYPTION_PUBLIC_KEY_DIR=public_dir,
        PHOTO_DIR=tmp_path / "photos",
        ALLOW_INSECURE_INTERNAL_TESTS=True,
    )
    repo = FakeRepository()
    return TestClient(create_app(repo=repo, config=config)), repo, config


def make_secure_client(tmp_path):
    request_private = ed25519.Ed25519PrivateKey.generate()
    response_private = ed25519.Ed25519PrivateKey.generate()
    request_public_path = tmp_path / "request-signing.pub"
    response_private_path = tmp_path / "response-signing.key"
    request_public_path.write_bytes(
        request_private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    response_private_path.write_bytes(
        response_private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    client, repo, config = make_client(tmp_path)
    config.ALLOW_INSECURE_INTERNAL_TESTS = False
    config.INTERNAL_REQUEST_PUBLIC_KEY_PATH = request_public_path
    config.INTERNAL_RESPONSE_PRIVATE_KEY_PATH = response_private_path
    return TestClient(create_app(repo=repo, config=config)), repo, request_private


def signed_headers(private_key, *, method="GET", path="/api/internal/status", request_id=None, nonce=None, timestamp=None):
    request_id = request_id or str(uuid.uuid4())
    nonce = nonce or "nonce-for-secure-tests-0001"
    timestamp = timestamp or str(int(time.time()))
    signature = b64encode(
        private_key.sign(canonical_request(method, path, timestamp, nonce, request_id, b""))
    )
    return {
        "X-Binhu-Client-Verify": "SUCCESS",
        "X-Binhu-Timestamp": timestamp,
        "X-Binhu-Nonce": nonce,
        "X-Binhu-Request-Id": request_id,
        "X-Binhu-Signature": signature,
    }


def jpeg_bytes():
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(output, format="JPEG")
    return output.getvalue()


def test_readiness_probe_checks_repository(tmp_path):
    client, _repo, _config = make_client(tmp_path)
    with client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_public_submission_is_encrypted_and_idempotent(tmp_path):
    client, repo, config = make_client(tmp_path)
    token = "venue-token-" + "x" * 32
    repo.venue = {
        "local_venue_id": 7,
        "display_name": "测试场所",
        "status": "active",
        "token_hmac": keyed_digest(config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", token),
    }
    submission_id = str(uuid.uuid4())

    with client:
        info = client.get(f"/api/public/venues/{token}")
        assert info.status_code == 200
        form_token = info.json()["form_token"]
        data = {
            "submission_id": submission_id,
            "venue_token": token,
            "form_token": form_token,
            "device_id": "device-id-for-tests-0001",
            "name": "测试人员",
            "identity_number": "11010519491231002X",
            "phone": "13800000000",
            "address": "测试地址",
        }
        first = client.post(
            "/api/public/submissions",
            data=data,
            files={"photo": ("photo.jpg", jpeg_bytes(), "image/jpeg")},
        )
        second = client.post(
            "/api/public/submissions",
            data=data,
            files={"photo": ("photo.jpg", jpeg_bytes(), "image/jpeg")},
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert len(repo.submissions) == 1
    assert list(config.PHOTO_DIR.glob("*.bin"))


def test_public_submission_rejects_bad_identity_checksum_before_consuming_token(tmp_path):
    client, repo, config = make_client(tmp_path)
    token = "venue-token-" + "v" * 32
    repo.venue = {
        "local_venue_id": 7,
        "display_name": "测试场所",
        "status": "active",
        "token_hmac": keyed_digest(config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", token),
    }
    with client:
        form_token = client.get(f"/api/public/venues/{token}").json()["form_token"]
        response = client.post(
            "/api/public/submissions",
            data={
                "submission_id": str(uuid.uuid4()),
                "venue_token": token,
                "form_token": form_token,
                "device_id": "device-id-for-validation",
                "name": "测试人员",
                "identity_number": "110105194912310020",
                "phone": "13800000000",
                "address": "测试 地址",
            },
            files={"photo": ("photo.jpg", jpeg_bytes(), "image/jpeg")},
        )
    assert response.status_code == 422
    assert "校验码" in response.json()["detail"]
    assert not repo.submissions
    assert repo.form_tokens


def test_registration_page_contains_client_side_format_checks_and_required_fields(tmp_path):
    client, repo, config = make_client(tmp_path)
    token = "validation-page-token-" + "p" * 32
    repo.venue = {
        "local_venue_id": 7,
        "display_name": "测试场所",
        "status": "active",
        "token_hmac": keyed_digest(config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", token),
    }
    with client:
        response = client.get(f"/venue/{token}")
    assert response.status_code == 200
    page = response.content.decode("utf-8")
    assert "required" in page
    assert "滨湖新城派出所" in page
    assert "滨湖智慧平台" not in page
    assert '<span class="label-text">姓名<span class="required"' in page
    assert page.count('class="label-text"') == 5
    assert page.count('class="required" aria-hidden="true"') == 5
    assert '.label-text{display:flex;align-items:baseline' in page
    assert "identityChecks" in page
    assert "^1[3-9]\\d{9}$" in page


def _drinking_payload(token, form_token, *, signature=None):
    signature = signature or [[{"x": 0.1, "y": 0.1}, {"x": 0.5, "y": 0.5}]]
    return {
        "submission_id": str(uuid.uuid4()),
        "form_token": form_token,
        "form_token_value": token,
        "device_id": "drinking-device-for-tests-0001",
        "name": "测试人员",
        "unit_position": "测试单位民警",
        "drinking_at": "2026-09-16T20:30",
        "drinking_place": "测试地点",
        "reason": "家庭聚会",
        "inviter": "测试邀约人",
        "travel_method": "公共交通",
        "responsible_leader_name": "测试领导",
        "notes": "虚构资料",
        "reporter_signature": signature,
        "leader_signature": signature,
    }


def test_drinking_page_and_three_second_server_gate(tmp_path):
    client, repo, config = make_client(tmp_path)
    token = "drinking-token-" + "d" * 32
    repo.public_form = {
        "form_key": "drinking_report",
        "display_name": "全所饮酒报备",
        "status": "active",
        "token_hmac": keyed_digest(config.PUBLIC_TOKEN_HMAC_KEY, "public-form-token", token),
    }
    with client:
        page = client.get(f"/drinking-report/{token}")
        info = client.get(f"/api/public/forms/{token}")
        response = client.post("/api/public/drinking-reports", json=_drinking_payload(token, info.json()["form_token"]))
    assert page.status_code == 200
    assert "苏州市公安局非工作日饮酒报备单" in page.text
    assert "reporter_signature" in page.text
    assert ".modal[hidden]{display:none}" in page.text
    assert page.text.count('class="label-text"') == 11
    assert page.text.count('class="required" aria-hidden="true"') == 10
    assert '.label-text{display:flex;align-items:baseline' in page.text
    assert '<span class="label-text">饮酒时间<span class="required"' in page.text
    assert 'class="signature-editor no-select"' in page.text
    assert 'data-signature-open="reporter"' in page.text
    assert 'data-signature-open="leader"' in page.text
    assert 'screen.orientation.lock' in page.text
    assert '保存签名' in page.text
    assert '请补充${missing.length}项必填内容' in page.text
    assert 'contextmenu' in page.text
    assert response.status_code == 400
    assert "重新扫码" in response.json()["detail"]
    assert not repo.submissions


def test_drinking_report_rejects_tap_only_signature_before_consuming_token(tmp_path):
    client, repo, config = make_client(tmp_path)
    token = "drinking-token-" + "s" * 32
    repo.public_form = {
        "form_key": "drinking_report",
        "display_name": "全所饮酒报备",
        "status": "active",
        "token_hmac": keyed_digest(config.PUBLIC_TOKEN_HMAC_KEY, "public-form-token", token),
    }
    with client:
        form_token = client.get(f"/api/public/forms/{token}").json()["form_token"]
        response = client.post(
            "/api/public/drinking-reports",
            json=_drinking_payload(token, form_token, signature=[[{"x": 0.2, "y": 0.2}, {"x": 0.2, "y": 0.2}]]),
        )
    assert response.status_code == 422
    assert "完整书写" in response.json()["detail"]
    assert repo.public_form_tokens
    assert not repo.submissions


def test_internal_venue_update_never_stores_raw_token(tmp_path):
    client, repo, config = make_client(tmp_path)
    raw_token = "raw-token-" + "z" * 32
    request_id = str(uuid.uuid4())
    with client:
        response = client.put(
            "/api/internal/venues/9",
            headers={"X-Binhu-Request-Id": request_id},
            json={
                "request_id": request_id,
                "display_name": "测试场所",
                "status": "active",
                "token": raw_token,
                "token_version": 1,
                "config_revision": 1,
            },
        )

    assert response.status_code == 200
    assert repo.venue["token_hmac"] == keyed_digest(config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", raw_token)
    assert raw_token not in repr(repo.venue)


def test_retired_or_disabled_token_returns_gone(tmp_path):
    client, repo, config = make_client(tmp_path)
    token = "retired-token-" + "r" * 32
    repo.venue = {
        "local_venue_id": 7,
        "display_name": "测试场所",
        "status": "retired",
        "token_hmac": keyed_digest(config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", token),
    }

    with client:
        response = client.get(f"/api/public/venues/{token}")

    assert response.status_code == 410
    assert "二维码已更换" in response.json()["detail"]


def test_retired_venue_page_returns_gone_html(tmp_path):
    client, repo, config = make_client(tmp_path)
    token = "retired-page-token-" + "r" * 32
    repo.venue = {
        "local_venue_id": 7,
        "display_name": "测试场所",
        "status": "retired",
        "token_hmac": keyed_digest(config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", token),
    }
    with client:
        response = client.get(f"/venue/{token}")
    assert response.status_code == 410
    assert "二维码已更换" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_registration_page_uses_uuid_fallback_for_legacy_webviews(tmp_path):
    client, repo, _config = make_client(tmp_path)
    token = "legacy-webview-token-" + "l" * 32
    repo.venue = {
        "local_venue_id": 7,
        "display_name": "测试场所",
        "status": "active",
        "token_hmac": keyed_digest(_config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", token),
    }

    with client:
        response = client.get(f"/venue/{token}")

    assert response.status_code == 200
    assert "const submissionId=makeUuid();" in response.text
    assert "deviceId=makeUuid()" in response.text
    assert "Date.now()}-0000-4000-8000" not in response.text


def test_wait_returns_immediately_when_queue_already_has_data(tmp_path):
    client, repo, _config = make_client(tmp_path)
    repo.submissions[str(uuid.uuid4())] = {
        "submission_id": str(uuid.uuid4()),
        "local_venue_id": 7,
        "request_fingerprint": "fingerprint",
        "state": "queued",
    }
    request_id = str(uuid.uuid4())

    with client:
        response = client.post(
            "/api/internal/submissions/wait",
            headers={"X-Binhu-Request-Id": request_id},
            json={"request_id": request_id, "worker_id": "binhu-primary", "timeout_seconds": 20},
        )

    assert response.status_code == 200
    assert response.json() == {"available": True, "pending_count": 1, "wake_reason": "available"}


def test_wait_times_out_without_available_data(tmp_path):
    client, _repo, _config = make_client(tmp_path)
    request_id = str(uuid.uuid4())

    with client:
        started = time.monotonic()
        response = client.post(
            "/api/internal/submissions/wait",
            headers={"X-Binhu-Request-Id": request_id},
            json={"request_id": request_id, "worker_id": "binhu-primary", "timeout_seconds": 1},
        )

    assert time.monotonic() - started >= 0.8
    assert response.status_code == 200
    assert response.json() == {"available": False, "pending_count": 0, "wake_reason": "timeout"}


def test_new_submission_wakes_waiting_worker(tmp_path):
    client, repo, config = make_client(tmp_path)
    token = "wake-token-" + "w" * 32
    repo.venue = {
        "local_venue_id": 7,
        "display_name": "测试场所",
        "status": "active",
        "token_hmac": keyed_digest(config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", token),
    }
    waiter_result = {}

    with client:
        form_token = client.get(f"/api/public/venues/{token}").json()["form_token"]

        def wait_for_signal():
            request_id = str(uuid.uuid4())
            waiter_result["response"] = client.post(
                "/api/internal/submissions/wait",
                headers={"X-Binhu-Request-Id": request_id},
                json={"request_id": request_id, "worker_id": "binhu-primary", "timeout_seconds": 5},
            )

        thread = threading.Thread(target=wait_for_signal)
        thread.start()
        time.sleep(0.1)
        submission = client.post(
            "/api/public/submissions",
            data={
                "submission_id": str(uuid.uuid4()),
                "venue_token": token,
                "form_token": form_token,
                "device_id": "device-id-for-wait-tests",
                "name": "测试人员",
                "identity_number": "11010519491231002X",
                "phone": "13800000000",
                "address": "测试地址",
            },
            files={"photo": ("photo.jpg", jpeg_bytes(), "image/jpeg")},
        )
        thread.join(timeout=3)

    assert submission.status_code == 202
    assert not thread.is_alive()
    response = waiter_result["response"]
    assert response.status_code == 200
    assert response.json() == {"available": True, "pending_count": 1, "wake_reason": "available"}


def test_internal_api_rejects_missing_mtls_and_invalid_or_expired_signatures(tmp_path):
    client, _repo, request_private = make_secure_client(tmp_path)
    wrong_private = ed25519.Ed25519PrivateKey.generate()
    with client:
        missing_mtls = client.get("/api/internal/status")
        invalid_signature = client.get("/api/internal/status", headers=signed_headers(wrong_private))
        expired_signature = client.get(
            "/api/internal/status",
            headers=signed_headers(request_private, timestamp=str(int(time.time()) - 301)),
        )

    assert missing_mtls.status_code == 401
    assert invalid_signature.status_code == 401
    assert expired_signature.status_code == 401


def test_internal_api_rejects_nonce_replay_and_signs_valid_response(tmp_path):
    client, _repo, request_private = make_secure_client(tmp_path)
    headers = signed_headers(request_private)
    with client:
        first = client.get("/api/internal/status", headers=headers)
        replay = client.get("/api/internal/status", headers=headers)

    assert first.status_code == 200
    assert first.headers.get("X-Binhu-Response-Timestamp")
    assert first.headers.get("X-Binhu-Response-Signature")
    assert replay.status_code == 409
