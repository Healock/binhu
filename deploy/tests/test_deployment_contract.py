from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DeploymentContractTests(unittest.TestCase):
    def test_production_workflow_is_manual_restricted_and_pinned(self) -> None:
        workflow = (ROOT / ".github/workflows/deploy-production.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotRegex(workflow, re.compile(r"(?m)^\s+push:\s*$"))
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("group: production-deploy", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("environment: production", workflow)
        self.assertIn(
            'git merge-base --is-ancestor "$release_commit" origin/main', workflow
        )
        self.assertIn('"binhu-deploy@$DEPLOY_HOST"', workflow)
        self.assertNotIn('"root@$DEPLOY_HOST"', workflow)
        self.assertIn("-o BatchMode=yes", workflow)
        self.assertIn("-o StrictHostKeyChecking=yes", workflow)
        self.assertIn("-o PasswordAuthentication=no", workflow)
        self.assertNotIn("docker compose", workflow)
        self.assertNotIn("docker build", workflow)

        action_refs = re.findall(r"uses:\s+[^@\s]+@([^\s#]+)", workflow)
        self.assertGreaterEqual(len(action_refs), 4)
        for action_ref in action_refs:
            self.assertRegex(action_ref, r"^[0-9a-f]{40}$")

    def test_ci_never_references_production_secrets_or_gateway(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request:", workflow)
        self.assertIn("push:", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("binhu-deploy@", workflow)

    def test_server_script_keeps_database_restore_out_of_automatic_path(self) -> None:
        script = (ROOT / "deploy/binhu-deploy").read_text(encoding="utf-8")
        self.assertIn("sync or backup task is active", script)
        self.assertIn("mysqldump", script)
        self.assertNotIn("docker compose down", script)
        self.assertNotIn("sha256sum -c", script)
        self.assertNotRegex(script, re.compile(r"mysql\s+[^\n]*<"))
        self.assertIn("rollback_after_error", script)
        self.assertIn("archive exceeds extraction limits", script)
        self.assertIn('[[ "$work_dir" == "$state_dir/work/"* ]]', script)

    def test_gateway_rejects_any_command_outside_fixed_grammar(self) -> None:
        gateway = ROOT / "deploy/binhu-deploy-gateway"
        for command in (
            "",
            "bash",
            "deploy 0.15.0 deadbeef none",
            "deploy 0.15.0 " + "a" * 40 + " invalid",
            "deploy 0.15.0 " + "a" * 40 + " none extra",
        ):
            with self.subTest(command=command):
                environment = os.environ.copy()
                environment["SSH_ORIGINAL_COMMAND"] = command
                result = subprocess.run(
                    ["bash", str(gateway)],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(64, result.returncode)


if __name__ == "__main__":
    unittest.main()
