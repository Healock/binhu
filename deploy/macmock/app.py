"""Small MAC mock service used by the offline residence client."""

from __future__ import annotations

import hmac
import os
import re
import tempfile
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)
MAC_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
DEFAULT_MAC = "02:00:00:00:00:01"
STATE_FILE = Path(os.environ.get("MACMOCK_STATE_FILE", "/data/mac-address"))
WRITE_TOKEN = os.environ.get("MACMOCK_WRITE_TOKEN", "")


def normalize_mac(value: str) -> str:
    compact = re.sub(r"[.\-:\s]", "", str(value or "")).upper()
    if not re.fullmatch(r"[0-9A-F]{12}", compact):
        raise ValueError("invalid_mac")
    mac = ":".join(compact[index:index + 2] for index in range(0, 12, 2))
    if not MAC_RE.fullmatch(mac) or mac == "00:00:00:00:00:00" or int(mac[:2], 16) & 1:
        raise ValueError("invalid_mac")
    return mac


def current_mac() -> str:
    try:
        return normalize_mac(STATE_FILE.read_text(encoding="ascii"))
    except (OSError, ValueError):
        try:
            return normalize_mac(os.environ.get("MAC_ADDRESS", DEFAULT_MAC))
        except ValueError:
            return DEFAULT_MAC


def write_mac(mac: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="mac-", dir=STATE_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(mac + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE_FILE)
        try:
            STATE_FILE.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Macmock-Token"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/", methods=["GET", "POST", "OPTIONS"])
def root():
    if request.method == "OPTIONS":
        return "", 204
    if request.method == "GET":
        return jsonify({"mac": current_mac()})
    if not WRITE_TOKEN or not hmac.compare_digest(
        request.headers.get("X-Macmock-Token", ""), WRITE_TOKEN
    ):
        return jsonify({"error": "write_forbidden"}), 403
    payload = request.get_json(silent=True)
    try:
        mac = normalize_mac(payload.get("mac") if isinstance(payload, dict) else "")
    except (AttributeError, ValueError):
        return jsonify({"error": "invalid_mac"}), 422
    write_mac(mac)
    return jsonify({"mac": mac}), 200


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host=os.environ.get("MACMOCK_BIND_HOST", "127.0.0.1"), port=int(os.environ.get("MACMOCK_PORT", "23333")))
