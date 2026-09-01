from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VenueCloudSecurityError(RuntimeError):
    pass


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_request(method: str, path: str, timestamp: str, nonce: str, request_id: str, body: bytes) -> bytes:
    return "\n".join(
        (method.upper(), path, timestamp, nonce, request_id, hashlib.sha256(body).hexdigest())
    ).encode("utf-8")


def load_request_signing_key(path: str) -> ed25519.Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise VenueCloudSecurityError("场所码云端请求签名密钥必须为 Ed25519")
    return key


def load_response_verify_key(path: str) -> ed25519.Ed25519PublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise VenueCloudSecurityError("场所码云端响应验签密钥必须为 Ed25519")
    return key


def sign_request(
    private_key: ed25519.Ed25519PrivateKey,
    *,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    request_id: str,
    body: bytes,
) -> str:
    return _b64encode(private_key.sign(canonical_request(method, path, timestamp, nonce, request_id, body)))


def verify_response(
    public_key: ed25519.Ed25519PublicKey,
    *,
    request_id: str,
    timestamp: str,
    signature: str,
    body: bytes,
) -> None:
    try:
        response_time = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise VenueCloudSecurityError("场所码云端响应时间戳无效") from exc
    if abs(int(time.time()) - response_time) > 300:
        raise VenueCloudSecurityError("场所码云端响应已过期")
    canonical = "\n".join((request_id, timestamp, hashlib.sha256(body).hexdigest())).encode("utf-8")
    try:
        public_key.verify(_b64decode(signature), canonical)
    except (InvalidSignature, ValueError) as exc:
        raise VenueCloudSecurityError("场所码云端响应签名无效") from exc


def decrypt_submission(item: dict, encrypted_photo: bytes, private_key_dir: str) -> tuple[dict, bytes]:
    if item.get("algorithm_version") != "rsa-oaep-sha256+aes-256-gcm-v1":
        raise VenueCloudSecurityError("不支持的场所码加密版本")
    key_id = str(item.get("key_id") or "")
    if not key_id or any(part in key_id for part in ("/", "\\", "..")):
        raise VenueCloudSecurityError("场所码加密 key_id 无效")
    key_path = (Path(private_key_dir).resolve() / f"{key_id}.pem").resolve()
    if Path(private_key_dir).resolve() not in key_path.parents or not key_path.is_file():
        raise VenueCloudSecurityError("场所码解密私钥不存在")
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
        raise VenueCloudSecurityError("场所码解密私钥类型无效")
    try:
        data_key = key.decrypt(
            _b64decode(str(item["wrapped_data_key"])),
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        aes = AESGCM(data_key)
        payload_bytes = aes.decrypt(
            _b64decode(str(item["payload_nonce"])),
            _b64decode(str(item["encrypted_payload"])),
            b"binhu-venue-payload-v1",
        )
        photo = aes.decrypt(
            _b64decode(str(item["photo_nonce"])),
            encrypted_photo,
            b"binhu-venue-photo-v1",
        )
        payload = json.loads(payload_bytes)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise VenueCloudSecurityError("场所码密文无法解密或校验") from exc
    if not isinstance(payload, dict):
        raise VenueCloudSecurityError("场所码解密正文结构无效")
    return payload, photo
