from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from deploy.environments.event_pipeline import deploy


class DevPipelineCandidateTests(unittest.TestCase):
    def digests(self):
        return {name: "sha256:" + (letter * 64) for name, letter in (
            ("mysql", "a"), ("redis", "b"), ("worker", "c"), ("flink", "d"))}

    def pipeline_jar(self, root):
        path = root / "dev-pipeline-job.jar"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("PipelineJob.class", b"test-bytecode")
        return path

    def test_build_and_verify_candidate_contains_only_fixed_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "candidate.tar.gz"
            pipeline_jar = self.pipeline_jar(Path(tmp))
            result = deploy.build(
                Path(__file__).resolve().parents[2], output,
                commit="a" * 40, run_id="dev-20260914-candidate1", pipeline_jar=pipeline_jar, **{
                    f"{name}_image": value for name, value in self.digests().items()
                },
            )
            self.assertTrue(result["ready_for_dev_pipeline"])
            checked = deploy.verify(output)
            self.assertTrue(checked["verified"])
            self.assertEqual(checked["run_id"], "dev-20260914-candidate1")
            self.assertEqual(set(checked["images"]), {"mysql", "redis", "worker", "flink"})

    def test_rejects_non_dev_identity_and_mutable_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "candidate.tar.gz"
            kwargs = {f"{name}_image": value for name, value in self.digests().items()}
            pipeline_jar = self.pipeline_jar(Path(tmp))
            with self.assertRaises(ValueError):
                deploy.build(Path(__file__).resolve().parents[2], output,
                             commit="a" * 40, run_id="production-20260914", pipeline_jar=pipeline_jar, **kwargs)
            with self.assertRaises(ValueError):
                deploy.build(Path(__file__).resolve().parents[2], output,
                             commit="a" * 40, run_id="dev-20260914-candidate2",
                             pipeline_jar=pipeline_jar,
                             **{**kwargs, "worker_image": "worker:latest"})

    def test_verify_rejects_tampered_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "candidate.tar.gz"
            kwargs = {f"{name}_image": value for name, value in self.digests().items()}
            pipeline_jar = self.pipeline_jar(Path(tmp))
            deploy.build(Path(__file__).resolve().parents[2], output,
                         commit="a" * 40, run_id="dev-20260914-candidate3", pipeline_jar=pipeline_jar, **kwargs)
            import tarfile
            tampered = Path(tmp) / "tampered.tar.gz"
            with tarfile.open(output, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
                for member in source.getmembers():
                    data = source.extractfile(member).read()
                    if member.name == deploy.MANIFEST:
                        manifest = json.loads(data)
                        manifest["environment"] = "production"
                        data = (json.dumps(manifest) + "\n").encode()
                    member.size = len(data)
                    target.addfile(member, __import__("io").BytesIO(data))
            with self.assertRaises(ValueError):
                deploy.verify(tampered)


if __name__ == "__main__":
    unittest.main()
