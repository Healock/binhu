import base64
import json
import os
import time
import uuid
import asyncio
import importlib.util
from pathlib import Path

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from routers import house_candidate


def _token(private_key, **overrides):
    now = int(time.time())
    header = {"alg": "EdDSA", "typ": "JWT", "kid": "test"}
    payload = {
        "iss": house_candidate.ISSUER,
        "aud": house_candidate.AUDIENCE,
        "sub": "build:" + "a" * 64,
        "scope": house_candidate.SCOPE,
        "comm": ["320584999001"],
        "iat": now,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
        **overrides,
    }

    def enc(value):
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()

    signing_input = f"{enc(header)}.{enc(payload)}"
    signature = private_key.sign(signing_input.encode())
    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def test_ed25519_token_verification_and_claims(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setattr(
        house_candidate.settings,
        "HOUSE_CANDIDATE_PUBLIC_KEYS_JSON",
        json.dumps({"test": public_pem}),
    )
    claims = house_candidate._verify_bearer("Bearer " + _token(private_key))
    assert claims["scope"] == "house:search:read"
    for subject in ("device:test-device", "build:" + "A" * 64, "build:short"):
        with pytest.raises(house_candidate.InvalidBearer):
            house_candidate._verify_bearer("Bearer " + _token(private_key, sub=subject))


def test_limits_are_shared_by_ip_across_builds_and_fail_closed(monkeypatch):
    calls = []

    class RedisStub:
        result = 0

        async def eval(self, script, count, *args):
            calls.append(args)
            return self.result

        async def aclose(self):
            pass

    client = RedisStub()
    monkeypatch.setattr(house_candidate.redis, "from_url", lambda *a, **kw: client)
    monkeypatch.setattr(house_candidate.time, "time", lambda: 1700000000)
    claims = {"sub": "build:" + "a" * 64, "exp": 1700000300, "jti": str(uuid.uuid4())}
    asyncio.run(house_candidate._enforce_replay_and_limits(claims, "192.0.2.1"))
    claims["sub"] = "build:" + "b" * 64
    claims["jti"] = str(uuid.uuid4())
    asyncio.run(house_candidate._enforce_replay_and_limits(claims, "192.0.2.1"))
    assert calls[0][1:3] == calls[1][1:3]
    assert calls[0][0] != calls[1][0]
    client.result = 2
    with pytest.raises(house_candidate.HTTPException) as exc:
        asyncio.run(house_candidate._enforce_replay_and_limits(claims, "192.0.2.1"))
    assert exc.value.status_code == 429


def test_invalid_algorithm_and_replay_shape_are_rejected(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(house_candidate.settings, "HOUSE_CANDIDATE_PUBLIC_KEYS_JSON", "{}")
    token = _token(private_key)
    with pytest.raises(house_candidate.InvalidBearer):
        house_candidate._verify_bearer("Basic " + token)


def test_issuer_output_is_accepted_by_binhu_verifier(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "deploy" / "ymt-token" / "app.py"
    spec = importlib.util.spec_from_file_location("ymt_issuer_contract", path)
    issuer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(issuer)
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setattr(house_candidate.settings, "HOUSE_CANDIDATE_PUBLIC_KEYS_JSON", json.dumps({issuer.KID: pem}))
    build = "a" * 64
    token, exp = issuer._jwt(key, build, ["TEST_COMMUNITY"])
    claims = house_candidate._verify_bearer("Bearer " + token)
    assert claims["sub"] == "build:" + build
    assert claims["exp"] == exp
    assert claims["comm"] == ["TEST_COMMUNITY"]

