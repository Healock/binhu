import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.security import EnvelopeEncryptor, b64decode, canonical_request, keyed_digest, request_fingerprint


def test_runtime_validation_accepts_encryption_key_directory(tmp_path):
    from app.config import Settings

    encryption_dir = tmp_path / "encryption-public"
    encryption_dir.mkdir()
    (encryption_dir / "key-1.pem").write_text("placeholder", encoding="utf-8")
    request_public = tmp_path / "request.pub"
    request_public.write_text("placeholder", encoding="utf-8")
    response_private = tmp_path / "response.key"
    response_private.write_text("placeholder", encoding="utf-8")

    settings = Settings(
        MYSQL_PASSWORD="test-password",
        PUBLIC_TOKEN_HMAC_KEY="a" * 32,
        FORM_TOKEN_HMAC_KEY="b" * 32,
        REQUEST_FINGERPRINT_KEY="c" * 32,
        ACTIVE_ENCRYPTION_KEY_ID="key-1",
        ENCRYPTION_PUBLIC_KEY_DIR=encryption_dir,
        INTERNAL_REQUEST_PUBLIC_KEY_PATH=request_public,
        INTERNAL_RESPONSE_PRIVATE_KEY_PATH=response_private,
    )

    settings.validate_runtime()


def test_envelope_encryption_round_trip(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public_dir = tmp_path / "keys"
    public_dir.mkdir()
    (public_dir / "key-2026-01.pem").write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    encrypted = EnvelopeEncryptor(public_dir, "key-2026-01").encrypt(b'{"name":"test"}', b"photo")
    data_key = private_key.decrypt(
        b64decode(encrypted.wrapped_data_key),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    aes = AESGCM(data_key)

    assert aes.decrypt(
        b64decode(encrypted.payload_nonce),
        b64decode(encrypted.encrypted_payload),
        b"binhu-venue-payload-v1",
    ) == b'{"name":"test"}'
    assert aes.decrypt(
        b64decode(encrypted.photo_nonce),
        encrypted.encrypted_photo,
        b"binhu-venue-photo-v1",
    ) == b"photo"


def test_request_fingerprint_is_keyed_and_content_sensitive():
    payload = {"name": "test", "phone": "13800000000"}
    first = request_fingerprint("x" * 32, payload, b"photo-a")
    second = request_fingerprint("x" * 32, payload, b"photo-b")
    other_key = request_fingerprint("y" * 32, payload, b"photo-a")

    assert first != second
    assert first != other_key
    assert len(first) == 64


def test_fixed_security_vectors():
    fixture = json.loads((Path(__file__).parent / "fixtures/security_vectors.json").read_text(encoding="utf-8"))
    request = fixture["canonical_request"]
    canonical = canonical_request(
        request["method"],
        request["path"],
        request["timestamp"],
        request["nonce"],
        request["request_id"],
        request["body"].encode(),
    )
    assert hashlib.sha256(canonical).hexdigest() == request["canonical_sha256"]
    digest = fixture["keyed_digest"]
    assert keyed_digest(digest["key_ascii"], digest["purpose"], digest["value"]) == digest["sha256"]
