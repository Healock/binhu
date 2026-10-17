import json
import base64
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

import app


def test_issue_token_for_enabled_build_without_real_secret_files(monkeypatch):
    key = Ed25519PrivateKey.generate()
    build = "a" * 64
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "key.pem").write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        (root / "builds.json").write_text(json.dumps({"builds": {build: {
            "version": "3.42.0", "comm": ["320584999001"], "enabled": True,
        }}}))
        monkeypatch.setattr(app, "PRIVATE_KEY_PATH", root / "key.pem")
        monkeypatch.setattr(app, "BUILDS_PATH", root / "builds.json")
        response = TestClient(app.app).post("/ymt/token", json={"build": build})
    assert response.status_code == 200
    assert set(response.json()) == {"token", "exp", "comm", "kid"}
    assert response.headers["cache-control"] == "no-store"
    claims = json.loads(base64.urlsafe_b64decode(response.json()["token"].split(".")[1] + "=="))
    assert claims["sub"] == "build:" + build


def test_unknown_or_disabled_build_is_forbidden(monkeypatch, tmp_path):
    build = "b" * 64
    path = tmp_path / "builds.json"
    path.write_text(json.dumps({"builds": {build: {
        "version": "3.42.0", "comm": ["320584999001"], "enabled": False,
    }}}))
    monkeypatch.setattr(app, "BUILDS_PATH", path)
    response = TestClient(app.app).post("/ymt/token", json={"build": build})
    assert response.status_code == 403
    assert response.json()["code"] == "BUILD_NOT_ALLOWED"


def test_unknown_method_and_invalid_body_are_safe():
    client = TestClient(app.app)
    assert client.get("/ymt/token").status_code == 405
    response = client.post("/ymt/token", content="not-json")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/ymt/token", json={"device": "6C92BF9B9A98", "secret": "x"}).status_code == 400
