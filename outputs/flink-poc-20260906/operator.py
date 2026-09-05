#!/usr/bin/env python3
"""Operate the isolated, synthetic Flink CDC POC on its target host.

This script deliberately fails closed before every Docker action.  It never
accepts a production database, an external network, a mutable image tag, or
an identity marker belonging to another run.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
MARK = ROOT / "identity-marker.json"
COMPOSE = ROOT / "docker-compose.yml"
SQL_INIT = ROOT / "mysql" / "init.sql"
SQL_RENDERED = ROOT / "mysql" / "init.rendered.sql"
SMOKE_SQL = ROOT / "sql" / "smoke.sql"
SMOKE_RENDERED = ROOT / "sql" / "smoke.rendered.sql"

PROJECT = "binhu-flink-poc-20260906"
DB = "FlinkPOC_20260906"
NET = "binhu_flink_poc_internal"
MYSQL_IMAGE = (
    "mysql@sha256:bdf3fd53f552ba36b7e6bc84a865075a629f411cd9594ce8bfd015351f7ef0ab"
)
REQUIRED_SERVICES = {"mysql", "jobmanager", "taskmanager"}
DIGEST_IMAGE = re.compile(r"^[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RUN_ID = re.compile(r"^FPOC-20260906-[A-Za-z0-9_-]{1,64}$")


def parse_env() -> dict[str, str]:
    if not ENV.is_file():
        raise SystemExit(".env is missing; run operator.py prepare first")
    values: dict[str, str] = {}
    for line_number, raw in enumerate(ENV.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"invalid .env line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise SystemExit(f"invalid or duplicate .env key at line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def run(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        check=False,
        capture_output=capture,
    )


def compose_args(*args: str) -> tuple[str, ...]:
    return (
        "docker",
        "compose",
        "--env-file",
        str(ENV),
        "-f",
        str(COMPOSE),
        "-p",
        PROJECT,
        *args,
    )


def compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return run(*compose_args(*args), capture=capture)


def require_digest(values: dict[str, str], key: str) -> None:
    value = values.get(key, "")
    if not DIGEST_IMAGE.fullmatch(value) or "@sha256:" not in value:
        raise SystemExit(f"{key} must be an exact image digest, never a tag")


def verify_static_boundary() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    if "internal: true" not in text:
        raise SystemExit("compose network is not marked internal")
    if re.search(r"(?m)^\s*ports\s*:", text):
        raise SystemExit("POC must not publish a host port")
    if "latest" in text.lower():
        raise SystemExit("mutable latest image tag is forbidden")


def validate_identity(values: dict[str, str]) -> str:
    verify_static_boundary()
    if values.get("POC_PROJECT") != PROJECT:
        raise SystemExit("POC project identity mismatch")
    if values.get("POC_NETWORK") != NET:
        raise SystemExit("POC network identity mismatch")
    if values.get("MYSQL_DATABASE") != DB:
        raise SystemExit("POC database identity mismatch")
    run_id = values.get("POC_RUN_ID", "")
    if not RUN_ID.fullmatch(run_id):
        raise SystemExit("POC_RUN_ID must be a unique FPOC-20260906-* value")
    if values.get("MYSQL_CDC_USER") != "flink_cdc":
        raise SystemExit("unexpected CDC user")
    if not values.get("MYSQL_ROOT_PASSWORD") or not values.get("MYSQL_CDC_PASSWORD"):
        raise SystemExit("POC passwords must be non-empty")
    require_digest(values, "MYSQL_IMAGE")
    require_digest(values, "FLINK_IMAGE")
    if not SHA256.fullmatch(values.get("CDC_SHA256", "")):
        raise SystemExit("CDC_SHA256 must be the verified 64-character Maven checksum")
    if not values.get("CDC_VERSION") or values.get("CDC_VERSION") == "latest":
        raise SystemExit("CDC_VERSION must be pinned")
    runtime_image = values.get("FLINK_RUNTIME_IMAGE", "")
    if not runtime_image or "latest" in runtime_image.lower():
        raise SystemExit("FLINK_RUNTIME_IMAGE must be a local, non-latest name")
    if not MARK.is_file():
        raise SystemExit("identity-marker.json is missing; run operator.py prepare")
    try:
        marker = json.loads(MARK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("identity-marker.json is invalid") from exc
    expected = {
        "project": PROJECT,
        "database": DB,
        "network": NET,
        "run_id": run_id,
        "synthetic": True,
    }
    if any(marker.get(key) != value for key, value in expected.items()):
        raise SystemExit("identity marker does not match this synthetic run")
    return run_id


def prepare() -> None:
    if ENV.exists() or MARK.exists() or SQL_RENDERED.exists():
        raise SystemExit("prepared POC files already exist; refusing overwrite")
    root_password = secrets.token_urlsafe(24)
    cdc_password = secrets.token_urlsafe(24)
    run_id = f"FPOC-20260906-{secrets.token_hex(6)}"
    ENV.write_text(
        "\n".join(
            [
                f"MYSQL_ROOT_PASSWORD={root_password}",
                f"MYSQL_CDC_PASSWORD={cdc_password}",
                f"MYSQL_DATABASE={DB}",
                "MYSQL_CDC_USER=flink_cdc",
                f"MYSQL_IMAGE={MYSQL_IMAGE}",
                "FLINK_IMAGE=",
                "FLINK_RUNTIME_IMAGE=binhu-flink-poc-runtime:20260906",
                "FLINK_VERSION=1.20.2",
                "CDC_VERSION=3.3.0",
                "CDC_SHA256=",
                f"POC_PROJECT={PROJECT}",
                f"POC_NETWORK={NET}",
                f"POC_RUN_ID={run_id}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(ENV, stat.S_IRUSR | stat.S_IWUSR)
    MARK.write_text(
        json.dumps(
            {
                "purpose": "synthetic-flink-cdc-poc",
                "project": PROJECT,
                "database": DB,
                "network": NET,
                "run_id": run_id,
                "synthetic": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    render_sql(parse_env())
    print("prepared random .env (0600), rendered SQL and synthetic identity marker")
    print("edit FLINK_IMAGE and CDC_SHA256 with values verified on the target host")


def render_sql(values: dict[str, str]) -> None:
    cdc_password = values.get("MYSQL_CDC_PASSWORD", "")
    run_id = values.get("POC_RUN_ID", "")
    if not cdc_password or not run_id:
        raise SystemExit("cannot render SQL without CDC password and run id")
    if any(token in cdc_password or token in run_id for token in ("\n", "\r", "'")):
        raise SystemExit("unsafe value in rendered SQL input")
    SQL_RENDERED.write_text(
        SQL_INIT.read_text(encoding="utf-8")
        .replace("__CDC_PASSWORD__", cdc_password)
        .replace("__POC_RUN_ID__", run_id),
        encoding="utf-8",
    )


def ensure_prepared() -> tuple[dict[str, str], str]:
    values = parse_env()
    run_id = validate_identity(values)
    render_sql(values)
    return values, run_id


def verify_container_identity(values: dict[str, str], run_id: str) -> None:
    network = run("docker", "network", "inspect", NET, "--format", "{{json .Labels}}", capture=True)
    if network.returncode != 0:
        raise SystemExit("shadow POC network is unavailable")
    try:
        labels = json.loads(network.stdout.strip())
    except json.JSONDecodeError as exc:
        raise SystemExit("shadow POC network labels are unreadable") from exc
    if labels.get("com.docker.compose.project") != PROJECT:
        raise SystemExit("network belongs to another Compose project")

    status = compose("ps", "--status", "running", "--services", capture=True)
    if status.returncode != 0:
        raise SystemExit("cannot inspect shadow POC containers")
    running = {line.strip() for line in status.stdout.splitlines() if line.strip()}
    if running != REQUIRED_SERVICES:
        raise SystemExit(f"expected running services {sorted(REQUIRED_SERVICES)}, got {sorted(running)}")

    query = (
        "mysql --protocol=socket -uroot -p\"$MYSQL_ROOT_PASSWORD\" -Nse "
        "'SELECT CONCAT(project_name, \"|\", database_name, \"|\", run_id, \"|\", synthetic) "
        "FROM poc_identity WHERE id=1'"
    )
    identity = compose("exec", "-T", "mysql", "sh", "-ec", query, capture=True)
    if identity.returncode != 0:
        raise SystemExit("cannot read the synthetic MySQL identity marker")
    expected = f"{PROJECT}|{DB}|{run_id}|1"
    if identity.stdout.strip() != expected:
        raise SystemExit("MySQL identity marker does not match this run")


def verify_existing_network() -> None:
    network = run("docker", "network", "inspect", NET, "--format", "{{json .Labels}}", capture=True)
    if network.returncode != 0:
        return
    try:
        labels = json.loads(network.stdout.strip())
    except json.JSONDecodeError as exc:
        raise SystemExit("existing POC network labels are unreadable") from exc
    if labels.get("com.docker.compose.project") != PROJECT:
        raise SystemExit("existing network belongs to another Compose project")


def verify(values: dict[str, str], run_id: str) -> int:
    config = compose("config", "--quiet", capture=True)
    if config.returncode != 0:
        return config.returncode
    verify_container_identity(values, run_id)
    print(f"verified isolated synthetic POC {PROJECT}, run {run_id}")
    return 0


def smoke(values: dict[str, str], run_id: str) -> int:
    verification = verify(values, run_id)
    if verification:
        return verification
    SMOKE_RENDERED.write_text(
        SMOKE_SQL.read_text(encoding="utf-8").replace(
            "__CDC_PASSWORD__", values["MYSQL_CDC_PASSWORD"]
        ),
        encoding="utf-8",
    )
    result = compose(
        "exec",
        "-T",
        "jobmanager",
        "/opt/flink/bin/sql-client.sh",
        "-f",
        "/opt/flink/sql/smoke.rendered.sql",
    )
    return result.returncode


def main() -> int:
    actions = {"prepare", "config", "up", "verify", "smoke", "stop"}
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        raise SystemExit("usage: operator.py prepare|config|up|verify|smoke|stop")
    action = sys.argv[1]
    if action == "prepare":
        prepare()
        return 0

    values, run_id = ensure_prepared()
    if action == "config":
        return compose("config", "--quiet").returncode
    if action == "up":
        config = compose("config", "--quiet", capture=True)
        if config.returncode:
            return config.returncode
        verify_existing_network()
        return compose("up", "-d", "--build").returncode
    if action == "verify":
        return verify(values, run_id)
    if action == "smoke":
        return smoke(values, run_id)
    # `down` keeps named volumes. Identity is checked before this operation so
    # a command from another run cannot stop an unrelated project by mistake.
    return compose("down").returncode


if __name__ == "__main__":
    sys.exit(main())
