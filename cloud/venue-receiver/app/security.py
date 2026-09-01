from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def keyed_digest(key: str, namespace: str, value: str) -> str:
    return hmac.new(key.encode("utf-8"), f"{namespace}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def request_fingerprint(key: str, payload: dict[str, str], photo: bytes) -> str:
    body = canonical_json(payload) + b"\0" + hashlib.sha256(photo).digest()
    return hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def canonical_request(method: str, path: str, timestamp: str, nonce: str, request_id: str, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join((method.upper(), path, timestamp, nonce, request_id, body_hash)).encode("utf-8")


def load_ed25519_public_key(path: Path) -> ed25519.Ed25519PublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise TypeError("内部请求签名公钥必须为 Ed25519")
    return key


def load_ed25519_private_key(path: Path) -> ed25519.Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise TypeError("内部响应签名私钥必须为 Ed25519")
    return key


def verify_request_signature(public_key: ed25519.Ed25519PublicKey, signature: str, canonical: bytes) -> None:
    public_key.verify(b64decode(signature), canonical)


def response_signature_headers(
    private_key: ed25519.Ed25519PrivateKey,
    *,
    request_id: str,
    timestamp: str,
    body: bytes,
) -> dict[str, str]:
    canonical = "\n".join((request_id, timestamp, hashlib.sha256(body).hexdigest())).encode("utf-8")
    return {
        "X-Binhu-Response-Timestamp": timestamp,
        "X-Binhu-Response-Signature": b64encode(private_key.sign(canonical)),
    }


@dataclass(frozen=True)
class EncryptedSubmission:
    algorithm_version: str
    key_id: str
    wrapped_data_key: str
    payload_nonce: str
    encrypted_payload: str
    photo_nonce: str
    encrypted_photo: bytes
    ciphertext_sha256: str
    photo_ciphertext_sha256: str


class EnvelopeEncryptor:
    def __init__(self, public_key_dir: Path, active_key_id: str):
        path = public_key_dir / f"{active_key_id}.pem"
        key = serialization.load_pem_public_key(path.read_bytes())
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
            raise TypeError("登记接收公钥必须为至少 3072 位 RSA 公钥")
        self._public_key = key
        self._key_id = active_key_id

    def encrypt(self, payload: bytes, photo: bytes) -> EncryptedSubmission:
        data_key = AESGCM.generate_key(bit_length=256)
        aes = AESGCM(data_key)
        payload_nonce = __import__("os").urandom(12)
        photo_nonce = __import__("os").urandom(12)
        encrypted_payload = aes.encrypt(payload_nonce, payload, b"binhu-venue-payload-v1")
        encrypted_photo = aes.encrypt(photo_nonce, photo, b"binhu-venue-photo-v1")
        wrapped = self._public_key.encrypt(
            data_key,
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        return EncryptedSubmission(
            algorithm_version="rsa-oaep-sha256+aes-256-gcm-v1",
            key_id=self._key_id,
            wrapped_data_key=b64encode(wrapped),
            payload_nonce=b64encode(payload_nonce),
            encrypted_payload=b64encode(encrypted_payload),
            photo_nonce=b64encode(photo_nonce),
            encrypted_photo=encrypted_photo,
            ciphertext_sha256=hashlib.sha256(encrypted_payload).hexdigest(),
            photo_ciphertext_sha256=hashlib.sha256(encrypted_photo).hexdigest(),
        )

