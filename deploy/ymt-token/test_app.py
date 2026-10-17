import base64
import hashlib
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

import app


def test_issue_and_disabled_device_without_real_secret_files(monkeypatch):
    key = Ed25519PrivateKey.generate()
    secret = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "key.pem").write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        (root / "devices.json").write_text(json.dumps({"devices": {"6C92BF9B9A98": {
            "secret_sha256": hashlib.sha256(b"s" * 32).hexdigest(), "comm": ["320584999001"], "enabled": True,
        }}}))
        monkeypatch.setattr(app, "PRIVATE_KEY_PATH", root / "key.pem")
        monkeypatch.setattr(app, "DEVICES_PATH", root / "devices.json")
        response = TestClient(app.app).post("/ymt/token", json={"device": "6c:92:bf:9b:9a:98", "secret": secret})
    assert response.status_code == 200
    assert set(response.json()) == {"token", "exp", "comm", "kid"}
    assert response.headers["cache-control"] == "no-store"


def test_unknown_method_and_invalid_body_are_safe():
    client = TestClient(app.app)
    assert client.get("/ymt/token").status_code == 405
    response = client.post("/ymt/token", content="not-json")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert response.headers["cache-control"] == "no-store"
