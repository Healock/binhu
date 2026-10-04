from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


TLS_LEVEL = 2


def _target(base_url: str) -> tuple[str, int]:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("staging_base_url_invalid")
    return parsed.hostname, parsed.port or 443


def _open_tls(host: str, port: int, context: ssl.SSLContext) -> bytes:
    raw = socket.create_connection((host, port), timeout=10)
    with raw:
        with context.wrap_socket(raw, server_hostname=host) as wrapped:
            return wrapped.getpeercert(binary_form=True)


def _parse_public_key_profile(details: str) -> tuple[str, int]:
    algorithm = re.search(r"Public Key Algorithm:\s*([^\r\n]+)", details)
    key_size = re.search(r"Public-Key:\s*\((\d+) bit\)", details)
    if not algorithm or not key_size:
        raise RuntimeError("peer_public_key_unrecognized")
    name = algorithm.group(1).strip().lower()
    bits = int(key_size.group(1))
    if "rsa" in name:
        kind = "RSA"
    elif "ec" in name:
        kind = "EC"
    else:
        kind = "OTHER"
    return kind, bits


def _public_key_profile(certificate_der: bytes) -> tuple[str, int]:
    result = subprocess.run(
        ["openssl", "x509", "-inform", "DER", "-noout", "-text"],
        input=certificate_der,
        capture_output=True,
        check=True,
    )
    return _parse_public_key_profile(result.stdout.decode("utf-8", errors="replace"))


def _certificate_identity(certificate_der: bytes) -> dict[str, str]:
    result = subprocess.run(
        ["openssl", "x509", "-inform", "DER", "-noout", "-subject", "-issuer", "-fingerprint", "-sha256"],
        input=certificate_der,
        capture_output=True,
        check=True,
        text=False,
    )
    identity: dict[str, str] = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("subject="):
            identity["subject"] = line.removeprefix("subject=").strip()
        elif line.startswith("issuer="):
            identity["issuer"] = line.removeprefix("issuer=").strip()
        elif line.startswith("sha256 Fingerprint="):
            identity["sha256_fingerprint"] = line.removeprefix("sha256 Fingerprint=").strip()
    if set(identity) != {"subject", "issuer", "sha256_fingerprint"}:
        raise RuntimeError("peer_certificate_identity_unrecognized")
    return identity


def _verified_context(level: int | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if level is not None:
        context.set_ciphers(f"DEFAULT:@SECLEVEL={level}")
    return context


def _verify(host: str, port: int, context: ssl.SSLContext) -> tuple[bool, str | None]:
    try:
        _open_tls(host, port, context)
        return True, None
    except ssl.SSLCertVerificationError as exc:
        message = (exc.verify_message or "").lower()
        if "ee certificate key too weak" in message:
            code = "ee_certificate_key_too_weak"
        elif "hostname mismatch" in message or "not valid for" in message:
            code = "hostname_mismatch"
        elif "self-signed" in message:
            code = "untrusted_certificate"
        elif "expired" in message:
            code = "certificate_expired"
        else:
            code = f"certificate_verify_code_{exc.verify_code}"
        return False, code
    except (OSError, ssl.SSLError) as exc:
        return False, type(exc).__name__.lower()


def _needs_level2(default_level: int, key_kind: str, key_bits: int, level2_valid: bool) -> bool:
    strong_key = (key_kind == "RSA" and key_bits >= 2048) or (key_kind == "EC" and key_bits >= 256)
    return default_level > TLS_LEVEL and strong_key and level2_valid


def _diagnose() -> dict:
    host, port = _target(os.environ.get("STAGING_BASE_URL", ""))
    openssl_cli = subprocess.run(
        ["openssl", "version"], capture_output=True, check=True, text=True,
    ).stdout.strip()
    resolved_addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})

    # This context is only used to read the peer's public leaf certificate; it
    # never sends an HTTP request and is never used by the load client.
    inspect_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    inspect_context.check_hostname = False
    inspect_context.verify_mode = ssl.CERT_NONE
    certificate = _open_tls(host, port, inspect_context)
    certificate_identity = _certificate_identity(certificate)
    key_kind, key_bits = _public_key_profile(certificate)

    default_context = _verified_context()
    level2_context = _verified_context(TLS_LEVEL)
    default_ok, default_error = _verify(host, port, default_context)
    level2_ok, level2_error = _verify(host, port, level2_context)
    strong_key = (key_kind == "RSA" and key_bits >= 2048) or (key_kind == "EC" and key_bits >= 256)
    action = "configure_level_2" if _needs_level2(default_context.security_level, key_kind, key_bits, level2_ok) else "no_change"
    return {
        "openssl_cli_version": openssl_cli,
        "openssl_version": ssl.OPENSSL_VERSION,
        "resolved_addresses": resolved_addresses,
        "default_security_level": default_context.security_level,
        "level_2_security_level": level2_context.security_level,
        "peer_public_key_type": key_kind,
        "peer_public_key_bits": key_bits,
        "peer_certificate_subject": certificate_identity["subject"],
        "peer_certificate_issuer": certificate_identity["issuer"],
        "peer_certificate_sha256": certificate_identity["sha256_fingerprint"],
        "peer_key_meets_minimum": strong_key,
        "default_chain_and_hostname_validation": "passed" if default_ok else "failed",
        "default_error_code": default_error,
        "level_2_chain_and_hostname_validation": "passed" if level2_ok else "failed",
        "level_2_error_code": level2_error,
        "recommended_action": action,
    }


def diagnose() -> dict:
    return _diagnose()


def configure(openssl_config: str, github_env: str) -> dict:
    report = _diagnose()
    configured = report["recommended_action"] == "configure_level_2"
    if configured:
        config_path = Path(openssl_config)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "openssl_conf = openssl_init\n"
            "[openssl_init]\n"
            "ssl_conf = ssl_config\n"
            "[ssl_config]\n"
            "system_default = tls_defaults\n"
            "[tls_defaults]\n"
            "CipherString = DEFAULT:@SECLEVEL=2\n",
            encoding="ascii",
        )
        config_path.chmod(0o600)
        with Path(github_env).open("a", encoding="utf-8") as stream:
            stream.write(f"OPENSSL_CONF={config_path}\n")
    report["runner_tls_configured"] = configured
    print(json.dumps(report, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("diagnose", "configure"))
    parser.add_argument("--openssl-config")
    parser.add_argument("--github-env")
    args = parser.parse_args()
    if args.operation == "diagnose":
        print(json.dumps(diagnose(), sort_keys=True))
        return
    if not args.openssl_config or not args.github_env:
        raise SystemExit("configure requires --openssl-config and --github-env")
    configure(args.openssl_config, args.github_env)


if __name__ == "__main__":
    main()
