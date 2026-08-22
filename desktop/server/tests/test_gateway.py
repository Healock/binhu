import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from desktop.server import binhu_update_gateway as gateway


def make_nupkg(path: Path, version: str) -> None:
    nuspec = f"""<?xml version="1.0"?><package><metadata><id>Binhu</id><version>{version}</version></metadata></package>"""
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("Binhu.nuspec", nuspec)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.environ["BINHU_UPDATE_ROOT"] = str(self.root)

    def tearDown(self):
        os.environ.pop("BINHU_UPDATE_ROOT", None)
        self.temp.cleanup()

    def bundle(self, version="0.25.15", commit="a" * 40, mutate=None, include_delta=None) -> bytes:
        source = self.root / "source"
        if source.exists():
            import shutil
            shutil.rmtree(source)
        source.mkdir()
        platforms = {}
        for platform in gateway.PLATFORMS:
            platform_root = source / platform
            platform_root.mkdir()
            full = platform_root / f"Binhu-{platform}-{version}-full.nupkg"
            make_nupkg(full, version)
            if include_delta is None:
                include_delta = version != "0.25.15"
            delta = None
            if include_delta:
                delta = platform_root / f"Binhu-{platform}-{version}-delta.nupkg"
                make_nupkg(delta, version)
            setup = platform_root / f"Binhu-{platform}-Setup.exe"
            setup.write_bytes(b"setup")
            assets = [{
                    "PackageId": f"com.bhzh.binhu.{platform}", "Version": version,
                    "Type": "Full", "FileName": full.name, "SHA1": "0" * 40,
                    "SHA256": digest(full), "Size": full.stat().st_size,
                }]
            if delta is not None:
                assets.append({
                    "PackageId": f"com.bhzh.binhu.{platform}", "Version": version,
                    "Type": "Delta", "FileName": delta.name, "SHA1": "0" * 40,
                    "SHA256": digest(delta), "Size": delta.stat().st_size,
                })
            feed = {"Assets": assets}
            (platform_root / "releases.stable.json").write_text(json.dumps(feed), encoding="utf-8")
            files = []
            for path in sorted(platform_root.iterdir()):
                files.append({"name": path.name, "size": path.stat().st_size, "sha256": digest(path)})
            platforms[platform] = {"files": files}
        manifest = {"schemaVersion": 1, "version": version, "commit": commit, "signed": False, "platforms": platforms}
        if mutate:
            mutate(source, manifest)
        (source / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            archive.add(source / "release.json", arcname="release.json")
            for platform in gateway.PLATFORMS:
                for path in (source / platform).iterdir():
                    archive.add(path, arcname=f"{platform}/{path.name}")
        return output.getvalue()

    def run_publish(self, data: bytes, version="0.25.15", commit="a" * 40, declared_hash=None):
        bundle_hash = declared_hash or hashlib.sha256(data).hexdigest()
        return gateway.main(["publish", version, commit, str(len(data)), bundle_hash], io.BytesIO(data))

    def test_publish_and_status(self):
        self.assertEqual(self.run_publish(self.bundle()), 0)
        for platform in gateway.PLATFORMS:
            self.assertTrue((self.root / "public" / platform / "releases.stable.json").is_file())
            state = json.loads((self.root / "state" / f"{platform}.json").read_text())
            self.assertEqual(state["version"], "0.25.15")
        self.assertTrue((self.root / "state" / "publish.lock").is_file())
        self.assertFalse((self.root / "publish.lock").exists())

    def test_rejects_bad_upload_hash(self):
        self.assertEqual(self.run_publish(self.bundle(), declared_hash="0" * 64), 1)

    def test_rejects_downgrade_and_same_version_different_commit(self):
        self.assertEqual(self.run_publish(self.bundle()), 0)
        self.assertEqual(self.run_publish(self.bundle("0.25.14", "b" * 40), "0.25.14", "b" * 40), 1)
        self.assertEqual(self.run_publish(self.bundle("0.25.15", "b" * 40), "0.25.15", "b" * 40), 1)

    def test_exact_release_retry_is_idempotent(self):
        data = self.bundle()
        self.assertEqual(self.run_publish(data), 0)
        self.assertEqual(self.run_publish(data), 0)
        for platform in gateway.PLATFORMS:
            state = json.loads((self.root / "state" / f"{platform}.json").read_text())
            self.assertEqual(state["version"], "0.25.15")
            self.assertEqual(state["commit"], "a" * 40)

    def test_rejects_manifest_hash_mismatch(self):
        def mutate(_source, manifest):
            manifest["platforms"]["win7-x64"]["files"][0]["sha256"] = "0" * 64
        self.assertEqual(self.run_publish(self.bundle(mutate=mutate)), 1)

    def test_rejects_path_traversal(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            info = tarfile.TarInfo("../escape")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        data = output.getvalue()
        self.assertEqual(self.run_publish(data), 1)
        self.assertFalse((self.root.parent / "escape").exists())

    def test_rejects_truncated_upload(self):
        data = self.bundle()
        result = gateway.main(
            ["publish", "0.25.15", "a" * 40, str(len(data) + 1), hashlib.sha256(data).hexdigest()],
            io.BytesIO(data),
        )
        self.assertEqual(result, 1)

    def test_requires_full_only_baseline_and_delta_after_baseline(self):
        self.assertEqual(self.run_publish(self.bundle(include_delta=True)), 1)
        self.assertEqual(self.run_publish(self.bundle()), 0)
        no_delta = self.bundle("0.25.16", "b" * 40, include_delta=False)
        self.assertEqual(self.run_publish(no_delta, "0.25.16", "b" * 40), 1)
        with_delta = self.bundle("0.25.16", "b" * 40, include_delta=True)
        self.assertEqual(self.run_publish(with_delta, "0.25.16", "b" * 40), 0)

    def test_empty_server_requires_02515_baseline(self):
        data = self.bundle("0.25.16", "b" * 40, include_delta=True)
        self.assertEqual(self.run_publish(data, "0.25.16", "b" * 40), 1)

    def test_fetch_only_current_full_package(self):
        self.assertEqual(self.run_publish(self.bundle()), 0)
        state = json.loads((self.root / "state" / "win7-x64.json").read_text())
        filename = state["fullPackage"]
        output = io.BytesIO()
        self.assertEqual(gateway.main(["fetch", "win7-x64", filename], stdout=output), 0)
        self.assertEqual(output.getvalue(), (self.root / "public" / "win7-x64" / filename).read_bytes())

    def test_fetch_rejects_other_files_and_platforms(self):
        self.assertEqual(self.run_publish(self.bundle()), 0)
        state = json.loads((self.root / "state" / "win7-x64.json").read_text())
        filename = state["fullPackage"]
        self.assertEqual(gateway.main(["fetch", "other", filename], stdout=io.BytesIO()), 1)
        self.assertEqual(gateway.main(["fetch", "win7-x64", "releases.stable.json"], stdout=io.BytesIO()), 1)
        self.assertEqual(gateway.main(["fetch", "win7-x64", "missing-full.nupkg"], stdout=io.BytesIO()), 1)


if __name__ == "__main__":
    unittest.main()
