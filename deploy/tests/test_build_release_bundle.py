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
            "full",
            output,
        )
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertEqual(manifest["commit"], self.commit)
        self.assertEqual(manifest["backup_scope"], "online")
        self.assertEqual(manifest["release_scope"], "full")

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
                "full",
                self.root / "release.tar.gz",
            )

    def test_backend_bundle_omits_frontend_and_unrelated_source(self):
        (self.repository / "backend").mkdir()
        (self.repository / "backend" / "main.py").write_text("print('ok')\n", encoding="utf-8")
        (self.repository / "frontend").mkdir()
        (self.repository / "frontend" / "source.ts").write_text("export {}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repository), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repository), "commit", "-qm", "backend"], check=True)
        commit = subprocess.check_output(
            ["git", "-C", str(self.repository), "rev-parse", "HEAD"],
            text=True,
        ).strip()

        output = self.root / "backend-release.tar.gz"
        manifest = build_bundle(
            self.repository,
            None,
            commit,
            "none",
            "backend",
            output,
        )

        self.assertEqual(manifest["release_scope"], "backend")
        self.assertEqual(set(manifest["files"]), {"source.tar.gz"})
        extracted = self.root / "backend-extracted"
        extracted.mkdir()
        with tarfile.open(output, "r:gz") as archive:
            self.assertEqual(
                sorted(archive.getnames()),
                ["SHA256SUMS", "manifest.json", "source.tar.gz"],
            )
            archive.extractall(extracted, filter="data")
        with tarfile.open(extracted / "source.tar.gz", "r:gz") as source:
            names = source.getnames()
            self.assertIn("VERSION", names)
            self.assertIn("backend/main.py", names)
            self.assertNotIn("frontend/source.ts", names)
            self.assertNotIn("tracked.txt", names)

    def test_full_release_requires_frontend_dist(self):
        with self.assertRaisesRegex(ValueError, "full release requires frontend dist"):
            build_bundle(
                self.repository,
                None,
                self.commit,
                "none",
                "full",
                self.root / "release.tar.gz",
            )

    def test_frontend_bundle_contains_only_version_and_dist(self):
        output = self.root / "frontend-release.tar.gz"
        manifest = build_bundle(
            self.repository,
            self.dist,
            self.commit,
            "none",
            "frontend",
            output,
        )

        self.assertEqual(manifest["release_scope"], "frontend")
        self.assertEqual(
            set(manifest["files"]),
            {"source.tar.gz", "frontend-dist.tar.gz"},
        )
        extracted = self.root / "frontend-extracted"
        extracted.mkdir()
        with tarfile.open(output, "r:gz") as archive:
            archive.extractall(extracted, filter="data")
        with tarfile.open(extracted / "source.tar.gz", "r:gz") as source:
            self.assertEqual(source.getnames(), ["VERSION"])
        with tarfile.open(extracted / "frontend-dist.tar.gz", "r:gz") as frontend:
            self.assertIn("dist/index.html", frontend.getnames())

    def test_frontend_release_requires_frontend_dist(self):
        with self.assertRaisesRegex(ValueError, "frontend release requires frontend dist"):
            build_bundle(
                self.repository,
                None,
                self.commit,
                "none",
                "frontend",
                self.root / "release.tar.gz",
            )


if __name__ == "__main__":
    unittest.main()
