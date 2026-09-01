import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.venue_cloud_security import VenueCloudSecurityError, decrypt_submission, verify_response


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def test_decrypt_submission_round_trip_and_unknown_key(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    key_id = "venue-key-2026-08"
    (tmp_path / f"{key_id}.pem").write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    data_key = AESGCM.generate_key(bit_length=256)
    aes = AESGCM(data_key)
    payload_nonce = os.urandom(12)
    photo_nonce = os.urandom(12)
    encrypted_payload = aes.encrypt(payload_nonce, b'{"name":"test"}', b"binhu-venue-payload-v1")
    encrypted_photo = aes.encrypt(photo_nonce, b"photo", b"binhu-venue-photo-v1")
    wrapped_key = private_key.public_key().encrypt(
        data_key,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    item = {
        "algorithm_version": "rsa-oaep-sha256+aes-256-gcm-v1",
        "key_id": key_id,
        "wrapped_data_key": _b64(wrapped_key),
        "payload_nonce": _b64(payload_nonce),
        "encrypted_payload": _b64(encrypted_payload),
        "photo_nonce": _b64(photo_nonce),
    }

    payload, photo = decrypt_submission(item, encrypted_photo, str(tmp_path))

    assert payload == {"name": "test"}
    assert photo == b"photo"
    with pytest.raises(VenueCloudSecurityError, match="私钥不存在"):
        decrypt_submission({**item, "key_id": "missing-key"}, encrypted_photo, str(tmp_path))


def test_verify_response_rejects_expired_timestamp():
    private_key = ed25519.Ed25519PrivateKey.generate()
    body = json.dumps({"status": "ok"}, separators=(",", ":")).encode()
    request_id = "00000000-0000-4000-8000-000000000001"
    timestamp = str(int(time.time()) - 301)
    canonical = "\n".join((request_id, timestamp, hashlib.sha256(body).hexdigest())).encode()
    signature = _b64(private_key.sign(canonical))

    with pytest.raises(VenueCloudSecurityError, match="已过期"):
        verify_response(
            private_key.public_key(),
            request_id=request_id,
            timestamp=timestamp,
            signature=signature,
            body=body,
        )


def test_verify_response_rejects_forged_signature():
    trusted_key = ed25519.Ed25519PrivateKey.generate()
    forged_key = ed25519.Ed25519PrivateKey.generate()
    body = json.dumps({"status": "ok"}, separators=(",", ":")).encode()
    request_id = "00000000-0000-4000-8000-000000000002"
    timestamp = str(int(time.time()))
    canonical = "\n".join((request_id, timestamp, hashlib.sha256(body).hexdigest())).encode()
    signature = _b64(forged_key.sign(canonical))

    with pytest.raises(VenueCloudSecurityError, match="签名无效"):
        verify_response(
            trusted_key.public_key(),
            request_id=request_id,
            timestamp=timestamp,
            signature=signature,
            body=body,
        )
