"""Verify the fixed Dev Bootstrap identity without exposing response details.

This contract is intentionally shared by candidate health checks and release evidence.
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request


BOOTSTRAP_URL = "http://127.0.0.1:48125/api/app/bootstrap"
MAX_RESPONSE_BYTES = 64 * 1024
SEMVER = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
DEV_BOOTSTRAP_CONTRACT = {
    "environment": "development",
    "environment_id": "development",
    "api_entry": "/dev/api",
    "environment_label": "Dev 环境 · 虚构数据",
    "data_kind": "虚构或脱敏开发数据",
}


class BootstrapContractError(ValueError):
    """A safe reason code for a failed Bootstrap acceptance check."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BootstrapContractError("dev_bootstrap_redirect_refused")


def validate_payload(payload: object, expected_version: str) -> dict[str, object]:
    """Validate only the public identity contract and return a safe summary."""
    if not isinstance(expected_version, str) or not SEMVER.fullmatch(expected_version):
        raise BootstrapContractError("dev_bootstrap_expected_version_invalid")
    if not isinstance(payload, dict):
        raise BootstrapContractError("dev_bootstrap_payload_invalid")

    expected = {"server_version": expected_version, **DEV_BOOTSTRAP_CONTRACT}
    for field, value in expected.items():
        if payload.get(field) != value:
            raise BootstrapContractError(f"dev_bootstrap_{field}_mismatch")

    # Deliberately copy only public environment identity fields. Bootstrap may
    # also contain account permissions and other request-specific information.
    return {
        **expected,
        "bootstrap_url": BOOTSTRAP_URL,
        "contract_verified": True,
    }


def fetch_payload(opener=None) -> object:
    request = urllib.request.Request(
        BOOTSTRAP_URL,
        method="GET",
        headers={"Accept": "application/json"},
    )
    if opener is None:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
        )
    try:
        with opener.open(request, timeout=8) as response:
            if getattr(response, "status", 200) != 200:
                raise BootstrapContractError("dev_bootstrap_http_status_invalid")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except BootstrapContractError:
        raise
    except (OSError, urllib.error.URLError):
        raise BootstrapContractError("dev_bootstrap_request_failed") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise BootstrapContractError("dev_bootstrap_response_too_large")
    try:
        return json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise BootstrapContractError("dev_bootstrap_payload_invalid") from None


def verify(expected_version: str, opener=None) -> dict[str, object]:
    return validate_payload(fetch_payload(opener), expected_version)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verify", choices=("verify",))
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.expected_version), ensure_ascii=False, sort_keys=True))
    except BootstrapContractError as exc:
        raise SystemExit(exc.code) from None


if __name__ == "__main__":
    main()
