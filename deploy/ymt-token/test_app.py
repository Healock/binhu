import json
import base64
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

import app
import pytest


@pytest.fixture(autouse=True)
def isolated_rate_store(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "RATE_DB_PATH", tmp_path / "rate.sqlite3")
    monkeypatch.setattr(app, "ISSUANCE_ENABLED", True)


def test_signing_can_stay_disabled_with_key_provisioned(monkeypatch):
    monkeypatch.setattr(app, "ISSUANCE_ENABLED", False)
    response = TestClient(app.app).post("/ymt/token", json={"build": "a" * 64})
    assert response.status_code == 503
    assert response.json()["code"] == "SIGNING_UNAVAILABLE"


def test_ip_and_global_quotas_persist(monkeypatch):
    monkeypatch.setattr(app, "IP_RATE_LIMIT", 2)
    monkeypatch.setattr(app, "GLOBAL_DAILY_LIMIT", 3)
    assert app._take_rate("192.0.2.1")
    assert app._take_rate("192.0.2.1")
    assert not app._take_rate("192.0.2.1")
    assert app._take_rate("192.0.2.2")
    assert not app._take_rate("192.0.2.3")


def test_issue_token_for_enabled_build_without_real_secret_files(monkeypatch):
    key = Ed25519PrivateKey.generate()
    build = "a" * 64
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "key.pem").write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        (root / "builds.json").write_text(json.dumps({"builds": {build: {
            "version": "3.42.0",
            "comm": ["320584999001"],
            "communities": [{"code": "320584999001", "name": "冬梅"}],
            "enabled": True,
        }}}))
        monkeypatch.setattr(app, "PRIVATE_KEY_PATH", root / "key.pem")
        monkeypatch.setattr(app, "BUILDS_PATH", root / "builds.json")
        response = TestClient(app.app).post("/ymt/token", json={"build": build})
    assert response.status_code == 200
    assert set(response.json()) == {"token", "exp", "comm", "communities", "kid"}
    assert response.json()["comm"] == ["320584999001"]
    assert response.json()["communities"] == [{"code": "320584999001", "name": "冬梅"}]
    assert response.headers["cache-control"] == "no-store"
    claims = json.loads(base64.urlsafe_b64decode(response.json()["token"].split(".")[1] + "=="))
    assert claims["sub"] == "build:" + build
    assert claims["comm"] == response.json()["comm"]


@pytest.mark.parametrize(
    "communities",
    [
        [],
        [{"code": "320584999002", "name": "邻里"}],
        [
            {"code": "320584999001", "name": "冬梅"},
            {"code": "320584999001", "name": "冬梅社区"},
        ],
        [{"code": "320584999001", "name": " "}],
        [{"code": "320584999001", "name": "冬梅", "extra": "ignored?"}],
    ],
)
def test_invalid_community_mapping_fails_closed(monkeypatch, tmp_path, communities):
    build = "d" * 64
    path = tmp_path / "builds.json"
    path.write_text(json.dumps({"builds": {build: {
        "version": "3.43.1",
        "comm": ["320584999001"],
        "communities": communities,
        "enabled": True,
    }}}))
    monkeypatch.setattr(app, "BUILDS_PATH", path)
    response = TestClient(app.app).post("/ymt/token", json={"build": build})
    assert response.status_code == 500
    assert response.json()["code"] == "SIGNING_UNAVAILABLE"


def test_unknown_or_disabled_build_is_forbidden(monkeypatch, tmp_path):
    build = "b" * 64
    path = tmp_path / "builds.json"
    path.write_text(json.dumps({"builds": {build: {
        "version": "3.42.0",
        "comm": ["320584999001"],
        "communities": [{"code": "320584999001", "name": "冬梅"}],
        "enabled": False,
    }}}))
    monkeypatch.setattr(app, "BUILDS_PATH", path)
    response = TestClient(app.app).post("/ymt/token", json={"build": build})
    assert response.status_code == 403
    assert response.json()["code"] == "BUILD_NOT_ALLOWED"
    assert TestClient(app.app).post("/ymt/token", json={"build": "c" * 64}).status_code == 403


def test_unknown_method_and_invalid_body_are_safe():
    client = TestClient(app.app)
    assert client.get("/ymt/token").status_code == 405
    response = client.post("/ymt/token", content="not-json")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/ymt/token", json={"device": "6C92BF9B9A98", "secret": "x"}).status_code == 400
    assert client.post("/ymt/token", json={"build": "A" * 64}).status_code == 400
