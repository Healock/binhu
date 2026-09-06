from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/kafka-shadow/prepare_images.py"


def load_prepare_images() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prepare_images_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyntheticResponse:
    def __init__(self, body: bytes, digest: str) -> None:
        self.body = body
        self.headers = {"Docker-Content-Digest": digest}

    def __enter__(self) -> "SyntheticResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


class RedirectResponse(SyntheticResponse):
    def __init__(self, body: bytes, digest: str, url: str) -> None:
        super().__init__(body, digest)
        self.url = url

    def geturl(self) -> str:
        return self.url


def response(body: bytes) -> SyntheticResponse:
    return SyntheticResponse(body, "sha256:" + hashlib.sha256(body).hexdigest())


class PrepareImagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_prepare_images()

    def test_resolves_fixed_images_and_writes_lock_and_env(self) -> None:
        kafka_child = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"digest":"sha256:' + b"a" * 64 + b'"}}'
        apicurio_child = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"digest":"sha256:' + b"b" * 64 + b'"}}'
        kafka_child_digest = "sha256:" + hashlib.sha256(kafka_child).hexdigest()
        apicurio_child_digest = "sha256:" + hashlib.sha256(apicurio_child).hexdigest()
        kafka_index = json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": kafka_child_digest,
                        "platform": {"os": "linux", "architecture": "arm64"},
                    },
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": kafka_child_digest,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    },
                ],
            },
            separators=(",", ":"),
        ).encode()
        apicurio_index = json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": apicurio_child_digest,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()

        bodies = {
            "/v2/apache/kafka/manifests/3.9.0": response(kafka_index),
            f"/v2/apache/kafka/manifests/{kafka_child_digest}": response(kafka_child),
            "/v2/apicurio/apicurio-registry-mem/manifests/2.6.5.Final": response(apicurio_index),
            f"/v2/apicurio/apicurio-registry-mem/manifests/{apicurio_child_digest}": response(apicurio_child),
        }
        calls: list[tuple[str, float | None, str | None, str | None]] = []

        def opener(request: object, timeout: float | None = None) -> SyntheticResponse:
            url = getattr(request, "full_url")
            calls.append(
                (
                    url,
                    timeout,
                    request.get_header("Accept"),
                    request.get_header("User-agent"),
                )
            )
            path = url.removeprefix("https://docker.1panel.live")
            try:
                return bodies[path]
            except KeyError as exc:
                raise AssertionError(f"unexpected request {url}") from exc

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            result = self.module.prepare(
                output,
                opener=opener,
                generated_at="2026-09-06T00:00:00+00:00",
            )

            self.assertEqual(result["registry"], "docker.1panel.live")
            self.assertEqual(result["platform"], {"os": "linux", "architecture": "amd64"})
            lock_path = output / self.module.LOCK_FILENAME
            env_path = output / self.module.ENV_FILENAME
            self.assertTrue(lock_path.is_file())
            self.assertTrue(env_path.is_file())
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock["generated_at"], "2026-09-06T00:00:00+00:00")
            self.assertEqual(
                lock["images"]["KAFKA_IMAGE"]["image"],
                f"docker.1panel.live/apache/kafka@{kafka_child_digest}",
            )
            self.assertEqual(
                lock["images"]["APICURIO_IMAGE"]["image"],
                f"docker.1panel.live/apicurio/apicurio-registry-mem@{apicurio_child_digest}",
            )
            env = env_path.read_text(encoding="utf-8")
            self.assertIn(f"KAFKA_IMAGE=docker.1panel.live/apache/kafka@{kafka_child_digest}", env)
            self.assertIn(
                f"APICURIO_IMAGE=docker.1panel.live/apicurio/apicurio-registry-mem@{apicurio_child_digest}",
                env,
            )
            self.assertNotIn("PASSWORD", env)
            self.assertEqual(len(calls), 4)
            self.assertTrue(
                all(timeout == self.module.DEFAULT_TIMEOUT for _, timeout, _, _ in calls)
            )
            self.assertTrue(all(accept for _, _, accept, _ in calls))
            self.assertTrue(
                all(user_agent == self.module.USER_AGENT for _, _, _, user_agent in calls)
            )

    def test_digest_mismatch_leaves_no_output_files_or_temporary_files(self) -> None:
        body = b'{"schemaVersion":2,"manifests":[]}'
        bad = SyntheticResponse(body, "sha256:" + "0" * 64)

        def opener(_request: object, timeout: float | None = None) -> SyntheticResponse:
            self.assertEqual(timeout, self.module.DEFAULT_TIMEOUT)
            return bad

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(ValueError, "digest"):
                self.module.prepare(output, opener=opener)
            self.assertEqual(list(output.iterdir()), [])

    def test_manifest_size_limit_leaves_no_output_files(self) -> None:
        body = b"x" * (self.module.MAX_MANIFEST_BYTES + 1)
        oversized = response(body)

        def opener(_request: object, timeout: float | None = None) -> SyntheticResponse:
            self.assertEqual(timeout, self.module.DEFAULT_TIMEOUT)
            return oversized

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(ValueError, "byte limit"):
                self.module.prepare(output, opener=opener)
            self.assertEqual(list(output.iterdir()), [])

    def test_existing_output_is_rejected_before_network_access(self) -> None:
        calls = 0

        def opener(_request: object, timeout: float | None = None) -> SyntheticResponse:
            nonlocal calls
            calls += 1
            raise AssertionError("network must not be called when output exists")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            lock_path = output / self.module.LOCK_FILENAME
            lock_path.write_text("keep\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                self.module.prepare(output, opener=opener)
            self.assertEqual(lock_path.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(calls, 0)

    def test_output_directory_must_already_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaises(NotADirectoryError):
                self.module.prepare(missing, opener=lambda *_args, **_kwargs: None)

    def test_redirect_to_another_host_is_rejected_without_outputs(self) -> None:
        body = b'{"schemaVersion":2,"manifests":[]}'
        digest = "sha256:" + hashlib.sha256(body).hexdigest()

        def opener(_request: object, timeout: float | None = None) -> RedirectResponse:
            return RedirectResponse(body, digest, "https://unapproved.example/v2/redirected")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(ValueError, "approved registry"):
                self.module.prepare(output, opener=opener)
            self.assertEqual(list(output.iterdir()), [])

    def test_redirect_handler_rejects_cross_host_before_following(self) -> None:
        handler = self.module.ApprovedRegistryRedirectHandler()
        request = self.module.Request("https://docker.1panel.live/v2/test")
        with self.assertRaisesRegex(ValueError, "approved registry"):
            handler.redirect_request(
                request,
                object(),
                302,
                "Found",
                {},
                "https://unapproved.example/v2/test",
            )

    def test_atomic_publish_does_not_replace_a_racing_target(self) -> None:
        original_link = self.module.os.link

        def racing_link(source: object, target: object) -> None:
            target_path = Path(target)
            if target_path.name == self.module.ENV_FILENAME:
                target_path.write_bytes(b"racing output")
            original_link(source, target)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(self.module.os, "link", side_effect=racing_link):
                with self.assertRaises(FileExistsError):
                    self.module._write_outputs(output, b"lock", b"env")
            self.assertFalse((output / self.module.LOCK_FILENAME).exists())
            self.assertEqual(
                (output / self.module.ENV_FILENAME).read_bytes(),
                b"racing output",
            )
            self.assertEqual(
                list(output.glob(".prepare-images-*")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
