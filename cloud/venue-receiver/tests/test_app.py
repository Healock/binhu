import io
import sys
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.main import create_app
from app.security import keyed_digest


class FakeRepository:
    def __init__(self):
        self.venue = None
        self.form_tokens = set()
        self.submissions = {}

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

    async def get_submission(self, submission_id):
        return self.submissions.get(submission_id)

    async def create_submission(self, item):
        self.submissions[item["submission_id"]] = {
            "submission_id": item["submission_id"],
            "local_venue_id": item["local_venue_id"],
            "request_fingerprint": item["request_fingerprint"],
            "state": "queued",
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
            "identity_number": "32058419900101123X",
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
