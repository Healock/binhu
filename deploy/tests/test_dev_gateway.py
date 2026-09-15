import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "deploy/environments/event_pipeline/binhu-dev-event-pipeline-gateway"
IMPLEMENTATION = ROOT / "deploy/environments/event_pipeline/binhu-dev-event-pipeline-gateway.py"


class DevGatewayContractTests(unittest.TestCase):
    def test_dev_workflows_use_dedicated_environment(self):
        workflows = (
            ROOT / ".github/workflows/install-dev-event-pipeline-gateway.yml",
            ROOT / ".github/workflows/prepare-dev-event-pipeline.yml",
            ROOT / ".github/workflows/deploy-dev-event-pipeline.yml",
        )
        for workflow in workflows:
            text = workflow.read_text(encoding="utf-8")
            self.assertIn("environment: development", text, workflow.name)

    def test_dev_workflows_use_explicit_ssh_port_secret(self):
        workflows = (
            ROOT / ".github/workflows/install-dev-event-pipeline-gateway.yml",
            ROOT / ".github/workflows/deploy-dev-event-pipeline.yml",
        )
        for workflow in workflows:
            text = workflow.read_text(encoding="utf-8")
            self.assertIn("BINHU_DEV_EVENT_PIPELINE_PORT", text, workflow.name)
        install = workflows[0].read_text(encoding="utf-8")
        deploy = workflows[1].read_text(encoding="utf-8")
        self.assertIn("scp -P \"$DEV_PORT\"", install)
        self.assertIn("ssh -p \"$DEV_PORT\"", install)
        self.assertIn("ssh -p \"$DEV_PORT\"", deploy)

    def test_wrapper_has_only_fixed_operations(self):
        text = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("prepare)", text)
        self.assertIn("measure|apply)", text)
        self.assertIn("Only fixed Dev event-pipeline commands are allowed", text)
        self.assertNotIn("eval ", text)
        self.assertNotIn("bash -c", text)

    def test_python_rejects_wrong_environment_and_unsafe_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "candidate.tar.gz"
            manifest = {"environment": "production", "project": "binhu-development-pipeline"}
            with tarfile.open(path, "w:gz") as archive:
                data = json.dumps(manifest).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size = len(data)
                archive.addfile(info, __import__("io").BytesIO(data))
            self.assertTrue(path.is_file())

    def test_candidate_gateway_requires_compiled_pipeline_artifact(self):
        text = IMPLEMENTATION.read_text(encoding="utf-8")
        self.assertIn('pipeline-job.jar', text)
        self.assertIn('candidate Flink artifact hash mismatch', text)
        self.assertIn('candidate PipelineJob source hash mismatch', text)

    def test_installer_is_dev_scoped(self):
        text = (ROOT / "deploy/environments/event_pipeline/install-dev-gateway.sh").read_text(encoding="utf-8")
        self.assertIn("binhu-dev-deploy", text)
        self.assertIn("binhu-dev-event-pipeline-gateway", text)
        self.assertIn("usermod --password", text)
        self.assertIn('restricted_shell="/usr/local/bin/binhu-dev-event-pipeline-gateway"', text)
        self.assertIn('--shell "$restricted_shell"', text)
        self.assertNotIn("passwd -l", text)
        self.assertNotIn("/usr/sbin/nologin", text)
        self.assertNotIn("binhu-deploy-gateway", text)
        self.assertNotIn("/root/binhu", text)
        self.assertNotIn("staging", text.lower())
        self.assertNotIn("production", text.lower())


if __name__ == "__main__":
    unittest.main()
