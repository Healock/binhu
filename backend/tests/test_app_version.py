import os
import unittest
from unittest.mock import patch

from app_version import APP_VERSION, read_app_version


class AppVersionTests(unittest.TestCase):
    def test_repository_version_is_used(self):
        self.assertEqual(APP_VERSION, "0.1.0")

    def test_valid_environment_version_takes_priority(self):
        with patch.dict(
            os.environ,
            {"APP_VERSION": "1.2.3-beta.1+build.5"},
        ):
            self.assertEqual(read_app_version(), "1.2.3-beta.1+build.5")

    def test_invalid_environment_version_is_ignored(self):
        with patch.dict(os.environ, {"APP_VERSION": "latest"}):
            self.assertEqual(read_app_version(), "0.1.0")


if __name__ == "__main__":
    unittest.main()
