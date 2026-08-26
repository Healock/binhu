import tempfile
import unittest
import zipfile
import sys
import importlib.util
from pathlib import Path

_module_path = Path(__file__).with_name("validate-velopack-baseline.py")
_spec = importlib.util.spec_from_file_location("validate_velopack_baseline", _module_path)
_module = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_module)
validate = _module.validate


class ValidateBaselineTests(unittest.TestCase):
    def package(self, root: Path, name: str, package_id: str, version: str) -> Path:
        path = root / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "package.nuspec",
                f"<package><metadata><id>{package_id}</id><version>{version}</version></metadata></package>",
            )
        return path

    def test_accepts_matching_platform_and_version(self):
        with tempfile.TemporaryDirectory() as temp:
            package = self.package(
                Path(temp),
                "com.bhzh.binhu.win7.x64-0.26.3-stable-full.nupkg",
                "com.bhzh.binhu.win7.x64",
                "0.26.3",
            )
            validate("win7-x64", package, "0.26.3")

    def test_rejects_cross_platform_package(self):
        with tempfile.TemporaryDirectory() as temp:
            package = self.package(
                Path(temp),
                "com.bhzh.binhu.win10.x64-0.26.3-stable-full.nupkg",
                "com.bhzh.binhu.win10.x64",
                "0.26.3",
            )
            with self.assertRaisesRegex(ValueError, "does not belong"):
                validate("win7-x64", package, "0.26.3")

    def test_rejects_version_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            package = self.package(
                Path(temp),
                "com.bhzh.binhu.win7.x64-0.26.2-stable-full.nupkg",
                "com.bhzh.binhu.win7.x64",
                "0.26.2",
            )
            with self.assertRaisesRegex(ValueError, "does not match"):
                validate("win7-x64", package, "0.26.3")


if __name__ == "__main__":
    unittest.main()
