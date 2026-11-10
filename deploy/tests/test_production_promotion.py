from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from deploy.environments import production_promotion as promotion


class ProductionPromotionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.staging = self.root / "staging" / "candidates"
        self.candidate = self.staging / ("staging-app-" + "a" * 16)
        (self.candidate / "artifact").mkdir(parents=True)
        (self.candidate / "image").mkdir()
        self.production = self.root / "production" / "candidates"
        self.compose = self.root / "docker-compose.yml"
        self.compose.write_text("services:\n  backend:\n    image: binhu-backend\n", encoding="utf-8")
        self.compose_sha = hashlib.sha256(self.compose.read_bytes()).hexdigest()
        self.artifact_id = "b" * 64
        self.image_id = "sha256:" + "c" * 64
        self.commit = "d" * 40
        self.version = "1.2.3"
        self.run_id = self.candidate.name
        (self.candidate / "artifact" / "artifact.json").write_text("{}", encoding="utf-8")
        self.artifact_sha = hashlib.sha256(
            (self.candidate / "artifact" / "artifact.json").read_bytes()
        ).hexdigest()
        source_tar = self.candidate / "artifact" / "source.tar"
        with tarfile.open(source_tar, "w") as archive:
            for name, data in (("VERSION", b"1.2.3\n"),
                               ("backend/Dockerfile", b"FROM scratch\n"),
                               ("frontend/package.json", b"{}\n"),
                               ("frontend/package-lock.json", b"{}\n")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        frontend_tar = self.candidate / "artifact" / "frontend.tar"
        with tarfile.open(frontend_tar, "w") as archive:
            data = b"index"
            info = tarfile.TarInfo("dist/index.html")
            info.size = len(data)
            archive.addfile(info, __import__("io").BytesIO(data))
        (self.candidate / "image.json").write_text("{}", encoding="utf-8")
        accepted = {
            "environment": "staging", "run_id": self.run_id,
            "artifact_id": self.artifact_id, "image_id": self.image_id,
            "commit": self.commit, "version": self.version,
            "dev_acceptance_run_id": "dev-update-" + "e" * 16,
            "accepted": True, "production_modified": False,
        }
        ledger = {
            "schema": 1, **{key: value for key, value in accepted.items()
                             if key not in {"accepted", "production_modified"}},
            "artifact_manifest_sha256": self.artifact_sha,
            "prepared_at": 1, "state": "accepted",
        }
        (self.candidate / "promotion.json").write_text(json.dumps(ledger), encoding="utf-8")
        (self.candidate / "accept.json").write_text(json.dumps(accepted), encoding="utf-8")
        self.accepted = accepted
        self.ledger = ledger

    def tearDown(self):
        self.temporary.cleanup()

    def promote(self):
        return promotion.promote_accepted_candidate(
            run_id=self.run_id, artifact_id=self.artifact_id,
            artifact_sha256=self.artifact_sha, image_id=self.image_id,
            commit=self.commit, version=self.version,
            production_compose=self.compose, production_compose_sha256=self.compose_sha,
            staging_root=self.staging, production_root=self.production,
        )

    def promote_run(self, *, commit=None, version=None):
        return promotion.promote_accepted_run(
            run_id=self.run_id,
            expected_commit=commit or self.commit,
            expected_version=version or self.version,
            production_compose=self.compose,
            production_compose_sha256=self.compose_sha,
            staging_root=self.staging,
            production_root=self.production,
        )

    def run_with_verified_artifacts(self):
        manifest = {"artifact_id": self.artifact_id, "commit": self.commit,
                    "version": self.version}
        image = {"image_id": self.image_id}
        accepted_image = {"Id": self.image_id, "Config": {"Labels": {
            "binhu.artifact.id": self.artifact_id,
            "org.opencontainers.image.revision": self.commit,
            "org.opencontainers.image.version": self.version,
        }}}
        def save_image(image_id, archive):
            archive.write_bytes(b"docker image archive")
            return accepted_image
        with patch.object(promotion, "verify_artifact", return_value=manifest), \
                patch.object(promotion, "verify_image", return_value=image), \
                patch.object(promotion, "save_accepted_image", side_effect=save_image):
            return self.promote()

    def test_valid_acceptance_copies_exact_candidate_and_hashes_files(self):
        result = self.run_with_verified_artifacts()
        target = self.production / self.run_id
        self.assertEqual(result["image_id"], self.image_id)
        self.assertEqual(result["artifact_manifest_sha256"], self.artifact_sha)
        self.assertEqual((target / "artifact" / "source.tar").read_bytes(),
                         (self.candidate / "artifact" / "source.tar").read_bytes())
        self.assertEqual((target / "staging-ledger.json").read_text(encoding="utf-8"),
                         json.dumps(self.ledger))
        copied = target / "artifact" / "frontend.tar"
        self.assertEqual(result["files"]["artifact/frontend.tar"]["sha256"],
                         hashlib.sha256(copied.read_bytes()).hexdigest())
        self.assertEqual((target / "production-config" / "docker-compose.yml").read_bytes(),
                         self.compose.read_bytes())
        self.assertEqual((target / "image.tar").read_bytes(), b"docker image archive")
        self.assertEqual(result["files"]["image.tar"]["sha256"],
                         hashlib.sha256((target / "image.tar").read_bytes()).hexdigest())

    def test_gateway_entry_derives_candidate_identity_from_protected_ledger(self):
        manifest = {"artifact_id": self.artifact_id, "commit": self.commit,
                    "version": self.version}
        image = {"image_id": self.image_id}
        accepted_image = {"Id": self.image_id, "Config": {"Labels": {
            "binhu.artifact.id": self.artifact_id,
            "org.opencontainers.image.revision": self.commit,
            "org.opencontainers.image.version": self.version,
        }}}
        def save_image(image_id, archive):
            archive.write_bytes(b"docker image archive")
            return accepted_image
        with patch.object(promotion, "verify_artifact", return_value=manifest), \
                patch.object(promotion, "verify_image", return_value=image), \
                patch.object(promotion, "save_accepted_image", side_effect=save_image):
            result = self.promote_run()
        self.assertEqual(result["staging_run_id"], self.run_id)
        self.assertEqual(result["artifact_id"], self.artifact_id)
        self.assertEqual(result["image_id"], self.image_id)

    def test_gateway_entry_rejects_commit_or_version_mismatch_before_copying(self):
        for commit, version in (("f" * 40, self.version), (self.commit, "9.9.9")):
            with self.subTest(commit=commit, version=version), \
                    self.assertRaisesRegex(ValueError, "ledger_not_accepted"):
                self.promote_run(commit=commit, version=version)

    def test_retry_reuses_only_an_unchanged_promotion_candidate(self):
        result = self.run_with_verified_artifacts()
        manifest = {"artifact_id": self.artifact_id, "commit": self.commit,
                    "version": self.version}
        image = {"image_id": self.image_id}
        with patch.object(promotion, "verify_artifact", return_value=manifest), \
                patch.object(promotion, "verify_image", return_value=image), \
                patch.object(promotion, "save_accepted_image") as save:
            retried = self.promote()
        self.assertEqual(retried, result)
        save.assert_not_called()
        (self.production / self.run_id / "image.tar").write_bytes(b"tampered")
        with patch.object(promotion, "verify_artifact", return_value=manifest), \
                patch.object(promotion, "verify_image", return_value=image), \
                patch.object(promotion, "save_accepted_image"):
            with self.assertRaisesRegex(ValueError, "production_candidate_digest_mismatch"):
                self.promote()

    def test_failed_candidate_build_never_publishes_a_partial_directory(self):
        with patch.object(promotion, "verify_artifact", return_value={
                "artifact_id": self.artifact_id, "commit": self.commit,
                "version": self.version}), \
                patch.object(promotion, "verify_image", return_value={"image_id": self.image_id}), \
                patch.object(promotion, "save_accepted_image", side_effect=ValueError("save failed")):
            with self.assertRaisesRegex(ValueError, "save failed"):
                self.promote()

        self.assertFalse((self.production / self.run_id).exists())
        self.assertEqual(list(self.production.glob(f".{self.run_id}.partial-*")), [])

    def test_save_uses_the_exact_accepted_image_id(self):
        archive = self.root / "image.tar"
        inspect = {"Id": self.image_id, "Config": {"Labels": {}}}
        completed = type("Completed", (), {"returncode": 0})()
        def save(command, **kwargs):
            Path(command[4]).write_bytes(b"accepted image")
            return completed
        with patch.object(promotion.subprocess, "run", side_effect=save) as run, \
                patch.object(promotion, "inspect_image", return_value=inspect):
            image = promotion.save_accepted_image(self.image_id, archive)
        self.assertEqual(image["Id"], self.image_id)
        run.assert_called_once_with(
            ["docker", "image", "save", "--output", str(archive), self.image_id],
            capture_output=True, check=False, timeout=900,
        )

    def test_forged_accept_json_is_rejected_even_when_rehashed(self):
        forged = dict(self.accepted, accepted=True, artifact_id="f" * 64)
        (self.candidate / "accept.json").write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "acceptance_record_mismatch"):
            self.promote()

    def test_unaccepted_ledger_is_rejected(self):
        self.ledger["state"] = "applied"
        (self.candidate / "promotion.json").write_text(json.dumps(self.ledger), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ledger_not_accepted"):
            self.promote()

    def test_acceptance_false_is_rejected(self):
        (self.candidate / "accept.json").write_text(
            json.dumps(dict(self.accepted, accepted=False)), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "acceptance_record_mismatch"):
            self.promote()

    def test_transport_artifact_hash_must_match_ledger(self):
        self.ledger["artifact_manifest_sha256"] = "f" * 64
        (self.candidate / "promotion.json").write_text(json.dumps(self.ledger), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ledger_not_accepted"):
            self.promote()

    def test_package_identity_must_match_protected_ledger(self):
        with self.assertRaisesRegex(ValueError, "ledger_not_accepted"):
            promotion.promote_accepted_candidate(
                run_id=self.run_id, artifact_id=self.artifact_id,
                artifact_sha256=self.artifact_sha, image_id="sha256:" + "f" * 64,
                commit=self.commit, version=self.version,
                production_compose=self.compose, production_compose_sha256=self.compose_sha,
                staging_root=self.staging, production_root=self.production,
            )

    def test_compose_rejects_rebuild_commands_and_digest_mismatch(self):
        self.compose.write_text("services:\n  backend:\n    build: ./backend\n    image: binhu-backend\n",
                                encoding="utf-8")
        self.compose_sha = hashlib.sha256(self.compose.read_bytes()).hexdigest()
        manifest = {"artifact_id": self.artifact_id, "commit": self.commit,
                    "version": self.version}
        image = {"image_id": self.image_id}
        accepted_image = {"Id": self.image_id, "Config": {"Labels": {
            "binhu.artifact.id": self.artifact_id,
            "org.opencontainers.image.revision": self.commit,
            "org.opencontainers.image.version": self.version,
        }}}
        def save_image(image_id, archive):
            archive.write_bytes(b"docker image archive")
            return accepted_image
        with patch.object(promotion, "verify_artifact", return_value=manifest), \
                patch.object(promotion, "verify_image", return_value=image), \
                patch.object(promotion, "save_accepted_image", side_effect=save_image):
            with self.assertRaisesRegex(ValueError, "compose_contract_invalid"):
                self.promote()

    def test_saved_image_id_must_match_accepted_candidate(self):
        manifest = {"artifact_id": self.artifact_id, "commit": self.commit,
                    "version": self.version}
        image = {"image_id": self.image_id}
        def save_image(image_id, archive):
            archive.write_bytes(b"docker image archive")
            return {"Id": "sha256:" + "f" * 64, "Config": {"Labels": {}}}
        with patch.object(promotion, "verify_artifact", return_value=manifest), \
                patch.object(promotion, "verify_image", return_value=image), \
                patch.object(promotion, "save_accepted_image", side_effect=save_image):
            with self.assertRaisesRegex(ValueError, "staging_image_digest_mismatch"):
                self.promote()


if __name__ == "__main__":
    unittest.main()
