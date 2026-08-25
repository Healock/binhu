import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "package-android-release.py"
SPEC = importlib.util.spec_from_file_location("package_android_release", SCRIPT)
assert SPEC and SPEC.loader
package_android_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package_android_release)


class AndroidReleaseDigestTests(unittest.TestCase):
    def test_extracts_contiguous_digest(self):
        digest = "620E2C2822BE26999D08E9753CB76EC0DEA8B84D68C03A0465234B30AAA7FF59"
        output = f"Signer #1 certificate SHA-256 digest: {digest}\n"
        self.assertEqual(package_android_release.extract_sha256_digest(output), digest.lower())

    def test_extracts_colon_or_space_delimited_digest(self):
        digest = "620E2C2822BE26999D08E9753CB76EC0DEA8B84D68C03A0465234B30AAA7FF59"
        for separator in (":", " "):
            formatted = separator.join(digest[index:index + 2] for index in range(0, len(digest), 2))
            output = f"certificate SHA 256 fingerprint = {formatted}\n"
            self.assertEqual(package_android_release.extract_sha256_digest(output), digest.lower())

    def test_rejects_missing_or_ambiguous_digest(self):
        with self.assertRaises(SystemExit):
            package_android_release.extract_sha256_digest("Verified\n")
        with self.assertRaises(SystemExit):
            package_android_release.extract_sha256_digest(
                f"SHA-256: {'1' * 64}\nSHA-256: {'2' * 64}\n"
            )


if __name__ == "__main__":
    unittest.main()
