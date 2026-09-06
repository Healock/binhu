#!/usr/bin/env python3
"""Resolve and lock the approved Kafka shadow image manifests.

This utility only talks to the approved read-only registry mirror.  It never
invokes Docker, changes the Docker daemon, or writes credentials.  The caller
must provide an existing output directory; output files are never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


REGISTRY = "docker.1panel.live"
DEFAULT_TIMEOUT = 20.0
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
USER_AGENT = "binhu-shadow-image-preparer/1.0"
LOCK_FILENAME = "kafka-shadow-images.lock.json"
ENV_FILENAME = "kafka-shadow-images.env"
PLATFORM = {"os": "linux", "architecture": "amd64"}
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
IMAGE_SPECS: Mapping[str, tuple[str, str]] = {
    "KAFKA_IMAGE": ("apache/kafka", "3.9.0"),
    "APICURIO_IMAGE": ("apicurio/apicurio-registry-mem", "2.6.5.Final"),
}
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

Opener = Callable[..., Any]


def _validate_approved_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL is outside the approved registry") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != REGISTRY
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("URL is outside the approved registry")


class ApprovedRegistryRedirectHandler(HTTPRedirectHandler):
    """Reject a cross-host redirect before urllib follows it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_approved_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_with_redirect_guard(request: Request, *, timeout: float) -> Any:
    opener = build_opener(ApprovedRegistryRedirectHandler())
    return opener.open(request, timeout=timeout)


def _digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not DIGEST_PATTERN.fullmatch(value):
        raise ValueError(f"{label} is not a sha256 digest")
    return value


def _manifest_url(repository: str, reference: str) -> str:
    return f"https://{REGISTRY}/v2/{repository}/manifests/{reference}"


def _fetch_manifest(
    repository: str,
    reference: str,
    *,
    opener: Opener,
    timeout: float,
) -> tuple[bytes, str]:
    request_url = _manifest_url(repository, reference)
    _validate_approved_url(request_url)
    request = Request(
        request_url,
        headers={"Accept": ACCEPT, "User-Agent": USER_AGENT},
        method="GET",
    )
    with opener(request, timeout=timeout) as response:
        geturl = getattr(response, "geturl", None)
        response_url = geturl() if callable(geturl) else request_url
        _validate_approved_url(response_url)
        body = response.read(MAX_MANIFEST_BYTES + 1)
        header_digest = response.headers.get("Docker-Content-Digest")

    if not isinstance(body, bytes):
        raise ValueError(f"{repository}:{reference} returned a non-byte manifest body")
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError(
            f"manifest for {repository}:{reference} exceeds the "
            f"{MAX_MANIFEST_BYTES}-byte limit"
        )
    header_digest = _digest(
        header_digest,
        label=f"Docker-Content-Digest for {repository}:{reference}",
    )
    body_digest = "sha256:" + hashlib.sha256(body).hexdigest()
    if header_digest != body_digest:
        raise ValueError(
            f"manifest digest mismatch for {repository}:{reference}: "
            f"header {header_digest}, body {body_digest}"
        )
    return body, header_digest


def _json_object(body: bytes, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _resolve_image(
    env_key: str,
    repository: str,
    tag: str,
    *,
    opener: Opener,
    timeout: float,
) -> dict[str, Any]:
    index_body, index_digest = _fetch_manifest(
        repository,
        tag,
        opener=opener,
        timeout=timeout,
    )
    index = _json_object(index_body, label=f"manifest index for {env_key}")
    descriptors = index.get("manifests")
    if not isinstance(descriptors, list) or not descriptors:
        raise ValueError(f"manifest index for {env_key} has no child manifests")

    matches: list[dict[str, Any]] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            continue
        platform = descriptor.get("platform")
        if (
            isinstance(platform, dict)
            and platform.get("os") == PLATFORM["os"]
            and platform.get("architecture") == PLATFORM["architecture"]
        ):
            matches.append(descriptor)
    if len(matches) != 1:
        raise ValueError(
            f"manifest index for {env_key} has {len(matches)} linux/amd64 children; expected one"
        )

    child_digest = _digest(matches[0].get("digest"), label=f"{env_key} child digest")
    child_body, returned_child_digest = _fetch_manifest(
        repository,
        child_digest,
        opener=opener,
        timeout=timeout,
    )
    if returned_child_digest != child_digest:
        raise ValueError(
            f"{env_key} child digest does not match index descriptor: "
            f"descriptor {child_digest}, response {returned_child_digest}"
        )
    _json_object(child_body, label=f"manifest child for {env_key}")

    return {
        "repository": repository,
        "tag": tag,
        "index_digest": index_digest,
        "platform": dict(PLATFORM),
        "manifest_digest": child_digest,
        "image": f"{REGISTRY}/{repository}@{child_digest}",
    }


def _write_outputs(
    output_dir: Path,
    lock_bytes: bytes,
    env_bytes: bytes,
) -> None:
    targets = (output_dir / LOCK_FILENAME, output_dir / ENV_FILENAME)
    if any(os.path.lexists(path) for path in targets):
        existing = next(path for path in targets if os.path.lexists(path))
        raise FileExistsError(f"refusing to overwrite existing output: {existing}")

    temporary_paths: list[Path] = []
    published: list[tuple[Path, tuple[int, int]]] = []
    try:
        for suffix, content in ((".lock.tmp", lock_bytes), (".env.tmp", env_bytes)):
            fd, raw_path = tempfile.mkstemp(
                prefix=".prepare-images-",
                suffix=suffix,
                dir=output_dir,
            )
            temporary_path = Path(raw_path)
            temporary_paths.append(temporary_path)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

        # A hard link creates the target atomically and fails if a target
        # appeared after the preflight.  It cannot overwrite a racing file.
        for temporary_path, target in zip(temporary_paths, targets):
            temporary_stat = temporary_path.stat()
            os.link(temporary_path, target)
            published.append(
                (target, (temporary_stat.st_dev, temporary_stat.st_ino))
            )
            temporary_path.unlink()
    except Exception:
        for path, identity in published:
            try:
                current = path.stat()
                if (current.st_dev, current.st_ino) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        for path in temporary_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def prepare(
    output_dir: Path,
    *,
    opener: Opener | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Fetch, verify and atomically write the approved image lock files."""

    output_dir = Path(output_dir)
    if not output_dir.exists() or not output_dir.is_dir():
        raise NotADirectoryError(f"output directory must already exist: {output_dir}")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    targets = (output_dir / LOCK_FILENAME, output_dir / ENV_FILENAME)
    if any(os.path.lexists(path) for path in targets):
        existing = next(path for path in targets if os.path.lexists(path))
        raise FileExistsError(f"refusing to overwrite existing output: {existing}")

    fetcher = opener or _open_with_redirect_guard
    images = {
        env_key: _resolve_image(
            env_key,
            repository,
            tag,
            opener=fetcher,
            timeout=timeout,
        )
        for env_key, (repository, tag) in IMAGE_SPECS.items()
    }
    lock = {
        "schema": 1,
        "registry": REGISTRY,
        "platform": dict(PLATFORM),
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "images": images,
    }
    lock_bytes = (json.dumps(lock, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    env_bytes = (
        "# Candidate values for the isolated Kafka shadow Compose project.\n"
        "# Review the lock file and copy these values into the target host env.\n"
        f"KAFKA_IMAGE={images['KAFKA_IMAGE']['image']}\n"
        f"APICURIO_IMAGE={images['APICURIO_IMAGE']['image']}\n"
    ).encode("utf-8")
    _write_outputs(output_dir, lock_bytes, env_bytes)
    return lock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="existing directory that does not already contain the output files",
    )
    parser.add_argument(
        "--timeout",
        default=DEFAULT_TIMEOUT,
        type=float,
        help=f"urllib request timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    args = parser.parse_args(argv)
    try:
        lock = prepare(args.output_dir, timeout=args.timeout)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "lock": str(args.output_dir / LOCK_FILENAME),
                "env": str(args.output_dir / ENV_FILENAME),
                "registry": lock["registry"],
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
