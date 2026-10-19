import json
import base64
import hashlib
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption, PublicFormat

import app
import pytest


@pytest.fixture(autouse=True)
def isolated_rate_store(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "RATE_DB_PATH", tmp_path / "rate.sqlite3")
    monkeypatch.setattr(app, "ISSUANCE_ENABLED", True)
    _write_signed_release(monkeypatch, tmp_path, "3.43.1", b"verified extension build")


def _write_signed_release(monkeypatch, root, version, artifact):
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key()
    digest = hashlib.sha256(artifact).hexdigest()
    message = "\n".join((
        "zwzz-release-v1",
        f"version={version}",
        f"sha256={digest}",
        f"bytes={len(artifact)}",
    )).encode("utf-8")
    release = {
        "format": "zwzz-release-v1",
        "version": version,
        "sha256": digest,
        "bytes": len(artifact),
        "issued": "2026-10-19T19:58:11.402Z",
        "sig": base64.b64encode(key.sign(message)).decode("ascii"),
    }
    key_path = root / "release-public.pem"
    manifest_path = root / "release.json"
    artifact_path = root / "ymt-latest.js"
    policy_path = root / "release-policy.json"
    key_path.write_bytes(public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    manifest_path.write_text(json.dumps(release), encoding="utf-8")
    artifact_path.write_bytes(artifact)
    policy_path.write_text(json.dumps({
        "minimum_version": "3.43.0",
        "comm": ["320584999001"],
        "communities": [{"code": "320584999001", "name": "冬梅"}],
    }), encoding="utf-8")
    monkeypatch.setattr(app, "RELEASE_PUBLIC_KEY_PATH", key_path)
    monkeypatch.setattr(app, "RELEASE_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(app, "RELEASE_ARTIFACT_PATH", artifact_path)
    monkeypatch.setattr(app, "RELEASE_POLICY_PATH", policy_path)
    return release


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
            "version": "3.43.0",
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


def test_signed_release_can_issue_without_hash_allowlist(monkeypatch, tmp_path):
    release = _write_signed_release(monkeypatch, tmp_path, "3.43.1", b"signed release bytes")
    monkeypatch.setattr(app, "BUILDS_PATH", tmp_path / "empty-builds.json")
    (tmp_path / "empty-builds.json").write_text(json.dumps({"builds": {}}), encoding="utf-8")
    jwt_key = Ed25519PrivateKey.generate()
    private_key_path = tmp_path / "jwt-private.pem"
    private_key_path.write_bytes(jwt_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    monkeypatch.setattr(app, "PRIVATE_KEY_PATH", private_key_path)

    response = TestClient(app.app).post("/ymt/token", json={"build": release["sha256"]})

    assert response.status_code == 200
    assert response.json()["comm"] == ["320584999001"]
    assert response.json()["communities"] == [{"code": "320584999001", "name": "冬梅"}]
    claims = json.loads(base64.urlsafe_b64decode(response.json()["token"].split(".")[1] + "=="))
    assert claims["sub"] == "build:" + release["sha256"]
    assert claims["comm"] == response.json()["comm"]


def test_invalid_signed_release_blocks_legacy_allowlist(monkeypatch, tmp_path):
    legacy_build = "e" * 64
    builds_path = tmp_path / "builds.json"
    builds_path.write_text(json.dumps({"builds": {legacy_build: {
        "version": "3.43.0",
        "comm": ["320584999001"],
        "communities": [{"code": "320584999001", "name": "冬梅"}],
        "enabled": True,
    }}}), encoding="utf-8")
    monkeypatch.setattr(app, "BUILDS_PATH", builds_path)
    manifest = json.loads(app.RELEASE_MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["sig"] = base64.b64encode(b"x" * 64).decode("ascii")
    app.RELEASE_MANIFEST_PATH.write_text(json.dumps(manifest), encoding="utf-8")

    response = TestClient(app.app).post("/ymt/token", json={"build": legacy_build})

    assert response.status_code == 500
    assert response.json()["code"] == "SIGNING_UNAVAILABLE"


def test_signed_release_below_floor_is_forbidden(monkeypatch, tmp_path):
    release = _write_signed_release(monkeypatch, tmp_path, "3.43.0", b"old but signed")
    policy = json.loads(app.RELEASE_POLICY_PATH.read_text(encoding="utf-8"))
    policy["minimum_version"] = "3.43.1"
    app.RELEASE_POLICY_PATH.write_text(json.dumps(policy), encoding="utf-8")
    builds_path = tmp_path / "builds.json"
    builds_path.write_text(json.dumps({"builds": {release["sha256"]: {
        "version": "3.43.0",
        "comm": ["320584999001"],
        "communities": [{"code": "320584999001", "name": "冬梅"}],
        "enabled": True,
    }}}), encoding="utf-8")
    monkeypatch.setattr(app, "BUILDS_PATH", builds_path)

    response = TestClient(app.app).post("/ymt/token", json={"build": release["sha256"]})

    assert response.status_code == 403
    assert response.json()["code"] == "BUILD_NOT_ALLOWED"


def test_legacy_build_below_floor_is_forbidden(monkeypatch, tmp_path):
    legacy_build = "f" * 64
    policy = json.loads(app.RELEASE_POLICY_PATH.read_text(encoding="utf-8"))
    policy["minimum_version"] = "3.43.1"
    app.RELEASE_POLICY_PATH.write_text(json.dumps(policy), encoding="utf-8")
    builds_path = tmp_path / "builds.json"
    builds_path.write_text(json.dumps({"builds": {legacy_build: {
        "version": "3.43.0",
        "comm": ["320584999001"],
        "communities": [{"code": "320584999001", "name": "冬梅"}],
        "enabled": True,
    }}}), encoding="utf-8")
    monkeypatch.setattr(app, "BUILDS_PATH", builds_path)

    response = TestClient(app.app).post("/ymt/token", json={"build": legacy_build})

    assert response.status_code == 403
    assert response.json()["code"] == "BUILD_NOT_ALLOWED"


def test_tampered_release_artifact_blocks_legacy_allowlist(monkeypatch, tmp_path):
    legacy_build = "9" * 64
    builds_path = tmp_path / "builds.json"
    builds_path.write_text(json.dumps({"builds": {legacy_build: {
        "version": "3.43.0",
        "comm": ["320584999001"],
        "communities": [{"code": "320584999001", "name": "冬梅"}],
        "enabled": True,
    }}}), encoding="utf-8")
    monkeypatch.setattr(app, "BUILDS_PATH", builds_path)
    app.RELEASE_ARTIFACT_PATH.write_bytes(b"tampered")

    response = TestClient(app.app).post("/ymt/token", json={"build": legacy_build})

    assert response.status_code == 500
    assert response.json()["code"] == "SIGNING_UNAVAILABLE"


def test_pinned_publisher_key_is_ed25519():
    key = serialization.load_pem_public_key((Path(__file__).parent / "release-public.pem").read_bytes())
    assert isinstance(key, Ed25519PublicKey)
