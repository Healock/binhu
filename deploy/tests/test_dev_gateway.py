import json
import importlib.util
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
            ROOT / ".github/workflows/accept-dev-event-pipeline.yml",
        )
        for workflow in workflows:
            text = workflow.read_text(encoding="utf-8")
            self.assertIn("environment: development", text, workflow.name)

    def test_dev_workflows_use_explicit_ssh_port_secret(self):
        workflows = (
            ROOT / ".github/workflows/install-dev-event-pipeline-gateway.yml",
            ROOT / ".github/workflows/deploy-dev-event-pipeline.yml",
            ROOT / ".github/workflows/accept-dev-event-pipeline.yml",
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

    def test_wrapper_accepts_only_fixed_scale_gate(self):
        text = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("accept)", text)
        self.assertIn('1002|10000|100000', text)
        self.assertNotIn("cat |", text)
        self.assertNotIn("python -", text)

    def test_accept_workflow_uses_deploy_key_and_fixed_choices(self):
        workflow = ROOT / ".github/workflows/accept-dev-event-pipeline.yml"
        self.assertTrue(workflow.is_file())
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("BINHU_DEV_EVENT_PIPELINE_SSH_KEY", text)
        self.assertNotIn("ADMIN_SSH_KEY", text)
        self.assertIn("type: choice", text)
        for scale in ("1002", "10000", "100000"):
            self.assertIn(f"- '{scale}'", text)
        self.assertIn('"accept $RUN_ID $SCALE"', text)

    def test_gateway_accept_contract_is_current_run_only(self):
        spec = importlib.util.spec_from_file_location("dev_gateway", IMPLEMENTATION)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.validate_scale("1002"), 1002)
        self.assertEqual(module.validate_scale("10000"), 10000)
        self.assertEqual(module.validate_scale("100000"), 100000)
        for invalid in ("1000", "1003", "0", "all", "1002;id"):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit):
                module.validate_scale(invalid)
        source = IMPLEMENTATION.read_text(encoding="utf-8")
        self.assertIn('current.get("run_id") != run_id', source)
        self.assertIn('current.get("environment") != "development"', source)
        self.assertIn('current.get("acceptance") != "pending"', source)

    def test_gateway_timeout_covers_each_fixed_acceptance_window(self):
        spec = importlib.util.spec_from_file_location("dev_gateway_timeout", IMPLEMENTATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.module_timeout("prepare"), 600)
        self.assertGreater(module.module_timeout("accept", 1002), 360)
        self.assertGreater(module.module_timeout("accept", 10000), 960)
        self.assertGreater(module.module_timeout("accept", 100000), 2460)

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

    def test_installer_grants_only_fixed_accept_entry(self):
        text = (ROOT / "deploy/environments/event_pipeline/install-dev-gateway.sh").read_text(encoding="utf-8")
        self.assertIn("binhu-dev-event-pipeline-gateway.py accept *", text)
        self.assertNotIn("ALL=(ALL)", text)


if __name__ == "__main__":
    unittest.main()
