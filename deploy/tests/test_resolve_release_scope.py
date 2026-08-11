import subprocess
import tempfile
import unittest
from pathlib import Path

from deploy.resolve_release_scope import resolve_release_scope


class ResolveReleaseScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "repo"
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
        (self.repository / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (self.repository / "backend").mkdir()
        (self.repository / "backend" / "main.py").write_text("print('one')\n", encoding="utf-8")
        (self.repository / "frontend").mkdir()
        (self.repository / "frontend" / "app.ts").write_text("export {}\n", encoding="utf-8")
        self._commit("initial")
        self.deployed_commit = self._head()

    def tearDown(self):
        self.temporary.cleanup()

    def _commit(self, message: str) -> None:
        subprocess.run(["git", "-C", str(self.repository), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repository), "commit", "-qm", message], check=True)

    def _head(self) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repository), "rev-parse", "HEAD"],
            text=True,
        ).strip()

    def test_auto_selects_backend_for_backend_docs_and_version(self):
        (self.repository / "backend" / "main.py").write_text("print('two')\n", encoding="utf-8")
        (self.repository / "VERSION").write_text("1.0.1\n", encoding="utf-8")
        (self.repository / "docs").mkdir()
        (self.repository / "docs" / "operations.md").write_text("updated\n", encoding="utf-8")
        self._commit("backend")

        scope, unsafe = resolve_release_scope(
            self.repository, "auto", self.deployed_commit, self._head()
        )

        self.assertEqual(scope, "backend")
        self.assertEqual(unsafe, [])

    def test_auto_falls_back_to_full_for_frontend_change(self):
        (self.repository / "frontend" / "app.ts").write_text("export const value = 1\n", encoding="utf-8")
        self._commit("frontend")

        scope, unsafe = resolve_release_scope(
            self.repository, "auto", self.deployed_commit, self._head()
        )

        self.assertEqual(scope, "full")
        self.assertEqual(unsafe, ["frontend/app.ts"])

    def test_manual_backend_rejects_unsafe_change(self):
        (self.repository / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        self._commit("compose")

        with self.assertRaisesRegex(ValueError, "require a full release"):
            resolve_release_scope(
                self.repository, "backend", self.deployed_commit, self._head()
            )

    def test_manual_full_does_not_require_deployed_commit(self):
        scope, unsafe = resolve_release_scope(
            self.repository, "full", "unknown", self._head()
        )
        self.assertEqual(scope, "full")
        self.assertEqual(unsafe, [])

    def test_auto_falls_back_to_full_when_production_commit_is_unknown(self):
        scope, unsafe = resolve_release_scope(
            self.repository, "auto", "unknown", self._head()
        )
        self.assertEqual(scope, "full")
        self.assertEqual(unsafe, ["production commit could not be verified"])

    def test_manual_backend_fails_when_production_commit_is_unknown(self):
        with self.assertRaisesRegex(ValueError, "deployed commit is unavailable"):
            resolve_release_scope(
                self.repository, "backend", "unknown", self._head()
            )


if __name__ == "__main__":
    unittest.main()
