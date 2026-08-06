import hashlib
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from deploy.build_release_bundle import build_bundle


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repository)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repository), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repository), "config", "user.name", "Test"],
            check=True,
        )
        (self.repository / "VERSION").write_text("1.2.3\n", encoding="utf-8")
        (self.repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repository), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repository), "commit", "-qm", "test"],
            check=True,
        )
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.repository), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        (self.repository / "not-tracked-secret.txt").write_text("secret", encoding="utf-8")
        self.dist = self.root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text("<script src='/assets/app.js'></script>", encoding="utf-8")
        (self.dist / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_bundle_is_bound_to_commit_version_scope_and_checksums(self):
        output = self.root / "release.tar.gz"
        manifest = build_bundle(
            self.repository,
            self.dist,
            self.commit,
            "online",
            output,
        )
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertEqual(manifest["commit"], self.commit)
        self.assertEqual(manifest["backup_scope"], "online")

        extracted = self.root / "extracted"
        extracted.mkdir()
        with tarfile.open(output, "r:gz") as archive:
            self.assertEqual(
                sorted(archive.getnames()),
                ["SHA256SUMS", "frontend-dist.tar.gz", "manifest.json", "source.tar.gz"],
            )
            archive.extractall(extracted, filter="data")
        saved_manifest = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_manifest, manifest)
        for filename, metadata in manifest["files"].items():
            digest = hashlib.sha256((extracted / filename).read_bytes()).hexdigest()
            self.assertEqual(digest, metadata["sha256"])

        with tarfile.open(extracted / "source.tar.gz", "r:gz") as source:
            self.assertIn("tracked.txt", source.getnames())
            self.assertNotIn("not-tracked-secret.txt", source.getnames())
        with tarfile.open(extracted / "frontend-dist.tar.gz", "r:gz") as frontend:
            self.assertIn("dist/index.html", frontend.getnames())

    def test_rejects_unknown_backup_scope(self):
        with self.assertRaisesRegex(ValueError, "unsupported backup scope"):
            build_bundle(
                self.repository,
                self.dist,
                self.commit,
                "everything",
                self.root / "release.tar.gz",
            )


if __name__ == "__main__":
    unittest.main()
