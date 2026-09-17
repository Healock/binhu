#!/usr/bin/python3.11
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path("/srv/binhu-venue")
COMPOSE = Path("/etc/binhu-venue/docker-compose.yml")
STATE = ROOT / "state"
NGINX_SOURCE = Path("/etc/binhu-venue/nginx-server-locations.conf")
NGINX_ACTIVE = Path("/www/server/panel/vhost/nginx/binhu-updates.conf")
NGINX_BACKUPS = STATE / "nginx-backups"


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def compose(env_file: Path, *args: str) -> None:
    run("docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE), *args)


def wait_for_health() -> None:
    for _ in range(45):
        result = subprocess.run(
            ("curl", "--fail", "--silent", "--show-error", "--max-time", "10", "http://127.0.0.1:48727/health/ready"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(2)
    fail("receiver health check failed")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_nginx_config(unpacked: Path, expected_sha: str) -> Path:
    source = unpacked / "nginx-server-locations.conf"
    if not source.is_file() or sha256_file(source) != expected_sha:
        fail("nginx include hash mismatch")
    if not NGINX_SOURCE.is_file() or not NGINX_ACTIVE.is_file():
        fail("required Nginx configuration is missing")
    backup = NGINX_BACKUPS / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    shutil.copy2(NGINX_SOURCE, backup / "nginx-server-locations.conf")
    shutil.copy2(NGINX_ACTIVE, backup / "binhu-updates.conf")
    try:
        staged = NGINX_SOURCE.with_name(NGINX_SOURCE.name + ".new")
        shutil.copy2(source, staged)
        os.replace(staged, NGINX_SOURCE)
        text = NGINX_ACTIVE.read_text(encoding="utf-8")
        include = f"    include {NGINX_SOURCE};"
        if include not in text:
            marker = "    location / { return 404; }"
            if text.count(marker) != 1:
                fail("existing Nginx server block marker not found exactly once")
            text = text.replace(marker, f"{include}\n\n{marker}", 1)
            staged_active = NGINX_ACTIVE.with_name(NGINX_ACTIVE.name + ".new")
            staged_active.write_text(text, encoding="utf-8")
            os.chmod(staged_active, NGINX_ACTIVE.stat().st_mode & 0o777)
            os.replace(staged_active, NGINX_ACTIVE)
        result = subprocess.run(("nginx", "-t"), capture_output=True, text=True, check=False)
        if result.returncode:
            fail("nginx configuration test failed")
        run("systemctl", "reload", "nginx")
        return backup
    except BaseException:
        shutil.copy2(backup / "nginx-server-locations.conf", NGINX_SOURCE)
        shutil.copy2(backup / "binhu-updates.conf", NGINX_ACTIVE)
        subprocess.run(("nginx", "-t"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        subprocess.run(("systemctl", "reload", "nginx"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        raise


def publish(commit: str, size: int, expected_sha: str) -> None:
    previous = STATE / "current.json"
    previous_data = json.loads(previous.read_text(encoding="utf-8")) if previous.is_file() else None
    if previous_data and previous_data.get("commit") == commit:
        fail("commit already published")
    with tempfile.TemporaryDirectory(dir=ROOT / "incoming") as temp_dir:
        bundle = Path(temp_dir) / "bundle.tar"
        digest = hashlib.sha256()
        remaining = size
        with bundle.open("wb") as output:
            while remaining:
                chunk = sys.stdin.buffer.read(min(1024 * 1024, remaining))
                if not chunk:
                    fail("truncated bundle")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        if sys.stdin.buffer.read(1):
            fail("bundle exceeds declared size")
        if digest.hexdigest() != expected_sha:
            fail("bundle sha256 mismatch")

        unpacked = Path(temp_dir) / "unpacked"
        unpacked.mkdir()
        with tarfile.open(bundle, "r:") as archive:
            members = archive.getmembers()
            if {member.name for member in members} != {"manifest.json", "image.tar", "nginx-server-locations.conf"}:
                fail("unexpected bundle contents")
            if any(not member.isfile() or Path(member.name).is_absolute() or ".." in Path(member.name).parts for member in members):
                fail("unsafe bundle member")
            archive.extractall(unpacked)
        manifest = json.loads((unpacked / "manifest.json").read_text(encoding="utf-8"))
        image = unpacked / "image.tar"
        image_digest = hashlib.sha256()
        with image.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                image_digest.update(chunk)
        expected_nginx_sha = manifest.get("nginx_locations_sha256")
        if manifest != {"commit": commit, "image": f"binhu-venue-receiver:{commit}", "image_sha256": image_digest.hexdigest(), "nginx_locations_sha256": expected_nginx_sha} or not re.fullmatch(r"[0-9a-f]{64}", str(expected_nginx_sha or "")):
            fail("manifest mismatch")
        run("docker", "load", "--input", str(image))
        candidate_env = STATE / f"candidate-{commit}.env"
        candidate_env.write_text(f"BINHU_VENUE_IMAGE_TAG={commit}\n", encoding="ascii")
        current_env = STATE / "current.env"
        nginx_backup = None
        try:
            compose(candidate_env, "up", "-d", "--remove-orphans")
            wait_for_health()
            nginx_backup = apply_nginx_config(unpacked, expected_nginx_sha)
        except BaseException:
            try:
                if previous_data:
                    rollback_env = STATE / "rollback.env"
                    rollback_env.write_text(f"BINHU_VENUE_IMAGE_TAG={previous_data['commit']}\n", encoding="ascii")
                    compose(rollback_env, "up", "-d", "--remove-orphans")
                    wait_for_health()
                    os.replace(rollback_env, current_env)
                else:
                    compose(candidate_env, "down", "--remove-orphans")
            finally:
                candidate_env.unlink(missing_ok=True)
            raise
        os.replace(candidate_env, current_env)
        staged = STATE / "current.json.new"
        staged.write_text(json.dumps({"status": "ok", "commit": commit}, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(staged, previous)
        archive_dir = ROOT / "archive" / commit
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(unpacked / "manifest.json", archive_dir / "manifest.json")
        print(json.dumps({"status": "published", "commit": commit, "nginx_config_backup": str(nginx_backup) if nginx_backup else None}))


def main() -> None:
    command = sys.argv[1:]
    if command == ["status"]:
        current = STATE / "current.json"
        print(current.read_text(encoding="utf-8") if current.is_file() else '{"status":"empty"}')
        return
    if len(command) != 4 or command[0] != "publish":
        fail("allowed commands: status or publish <commit> <size> <sha256>")
    commit, size_text, expected_sha = command[1:]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        fail("invalid commit")
    if not size_text.isdigit() or not 1 <= int(size_text) <= 1_500_000_000:
        fail("invalid bundle size")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        fail("invalid bundle sha256")
    publish(commit, int(size_text), expected_sha)


if __name__ == "__main__":
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "publish.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("another publish is in progress")
        main()
