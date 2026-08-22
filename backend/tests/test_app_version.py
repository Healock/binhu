import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app_version import APP_VERSION, read_app_version

REPOSITORY_VERSION = (Path(__file__).resolve().parents[2] / "VERSION").read_text(encoding="utf-8").strip()


class AppVersionTests(unittest.TestCase):
    def test_repository_version_is_used(self):
        self.assertEqual(APP_VERSION, REPOSITORY_VERSION)

    def test_valid_environment_version_takes_priority(self):
        with patch.dict(
            os.environ,
            {"APP_VERSION": "1.2.3-beta.1+build.5"},
        ):
            self.assertEqual(read_app_version(), "1.2.3-beta.1+build.5")

    def test_invalid_environment_version_is_ignored(self):
        with patch.dict(os.environ, {"APP_VERSION": "latest"}):
            self.assertEqual(read_app_version(), REPOSITORY_VERSION)


if __name__ == "__main__":
    unittest.main()
