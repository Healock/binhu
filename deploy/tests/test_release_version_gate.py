from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop" / "scripts"))

from validate_release_versions import validate_versions  # noqa: E402


class ReleaseVersionGateTests(unittest.TestCase):
    def test_allows_next_release_to_align_platforms_with_patch_skew(self) -> None:
        validate_versions("0.31.20", "0.31.18", "0.31.18", "0.31.19")

    def test_rejects_release_behind_any_published_platform(self) -> None:
        with self.assertRaisesRegex(ValueError, "downgrade"):
            validate_versions("0.31.18", "0.31.18", "0.31.18", "0.31.19")

    def test_rejects_inconsistent_windows_platforms(self) -> None:
        with self.assertRaisesRegex(ValueError, "Windows platform"):
            validate_versions("0.31.20", "0.31.18", "0.31.17", "0.31.19")


if __name__ == "__main__":
    unittest.main()
