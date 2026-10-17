import base64
import json
import os
import time
import uuid

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


def test_invalid_algorithm_and_replay_shape_are_rejected(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(house_candidate.settings, "HOUSE_CANDIDATE_PUBLIC_KEYS_JSON", "{}")
    token = _token(private_key)
    with pytest.raises(house_candidate.InvalidBearer):
        house_candidate._verify_bearer("Basic " + token)

