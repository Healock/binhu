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
        self.assertIn("release_scope:", workflow)
        self.assertIn("deploy/resolve_release_scope.py", workflow)
        self.assertIn("- frontend", workflow)
        self.assertIn("env.RELEASE_SCOPE == 'frontend' || env.RELEASE_SCOPE == 'full'", workflow)
        self.assertIn("if: env.RELEASE_SCOPE != 'frontend'", workflow)
        self.assertIn('"binhu-deploy@$DEPLOY_HOST" status', workflow)
        self.assertIn(
            'git merge-base --is-ancestor "$release_commit" origin/main', workflow
        )
        self.assertIn('"binhu-deploy@$DEPLOY_HOST"', workflow)
        self.assertNotIn('"root@$DEPLOY_HOST"', workflow)
        self.assertIn("-o BatchMode=yes", workflow)
        self.assertIn("-o StrictHostKeyChecking=yes", workflow)
        self.assertIn("-o PasswordAuthentication=no", workflow)
        self.assertIn(
            'bundle_size="$(wc -c < release-output/binhu-release.tar.gz)"',
            workflow,
        )
        self.assertIn('gateway_started="$(date +%s)"', workflow)
        self.assertIn("Fixed gateway upload and server deployment completed", workflow)
        self.assertIn(
            '"deploy $EXPECTED_VERSION $RELEASE_COMMIT $BACKUP_SCOPE $RELEASE_SCOPE $bundle_size"',
            workflow,
        )
        self.assertNotIn("BINHU_PUBLIC_URL", workflow)
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
        for database in (
            "OnlineData",
            "OnlineDataArchive",
            "daily_report",
            "PlatformData",
            "VisitData",
            "DispatchData",
            "RegistryData",
            "WorkflowData",
        ):
            self.assertIn(f"backup_database {database}", script)
        self.assertNotIn("docker compose down", script)
        self.assertNotIn("sha256sum -c", script)
        self.assertNotRegex(script, re.compile(r"mysql\s+[^\n]*<"))
        self.assertIn("rollback_after_error", script)
        self.assertIn("archive exceeds extraction limits", script)
        self.assertIn("BINHU_DEPLOY_PUBLIC_URL", script)
        self.assertIn("curl --http1.1 --fail", script)
        self.assertIn('[[ "$work_dir" == "$state_dir/work/"* ]]', script)
        self.assertIn('head -c "$expected_bundle_size"', script)
        self.assertIn('[[ "$bundle_size" == "$expected_bundle_size" ]]', script)
        self.assertIn('release_scope="full"', script)
        self.assertIn('RELEASE_SCOPE=%s', script)
        self.assertIn('release received in', script)
        self.assertIn('release validated and extracted in', script)
        self.assertIn('deployment preflight completed in', script)
        self.assertIn('database backup scope ${backup_scope} completed in', script)
        self.assertIn('application files switched in', script)
        self.assertIn('application containers recreated in', script)
        self.assertIn('deployment health checks completed in', script)
        self.assertIn('"timings_seconds"', script)
        for timing_name in (
            "RECEIVE",
            "VALIDATE_EXTRACT",
            "PREFLIGHT",
            "IMAGE_BUILD",
            "PROGRAM_BACKUP",
            "DATABASE_BACKUP",
            "SWITCH_FILES",
            "CONTAINER_RECREATE",
            "HEALTH_CHECKS",
        ):
            self.assertIn(f"DEPLOY_TIMING_{timing_name}_SECONDS", script)
        self.assertIn('if [[ "$status" == "success" ]]', script)
        self.assertIn('rm -rf -- "$project_dir/backend"', script)
        self.assertIn('release_scope" == "frontend"', script)
        self.assertIn('reusing current backend image for frontend release', script)
        self.assertIn('tar -czf "$program_backup" -C "$project_dir" VERSION frontend/dist', script)
        self.assertIn('switched=1', script)
        self.assertNotIn('BINHU_DEPLOY_MAX_BUNDLE_BYTES + 1', script)

    @unittest.skipIf(os.name == "nt", "受限部署网关测试需要 Linux bash")
    def test_gateway_rejects_any_command_outside_fixed_grammar(self) -> None:
        gateway = ROOT / "deploy/binhu-deploy-gateway"
        for command in (
            "",
            "bash",
            "deploy 0.15.0 deadbeef none",
            "deploy 0.15.0 " + "a" * 40 + " invalid",
            "deploy 0.15.0 " + "a" * 40 + " none",
            "deploy 0.15.0 " + "a" * 40 + " none 0",
            "deploy 0.15.0 " + "a" * 40 + " none 134217729",
            "deploy 0.15.0 " + "a" * 40 + " none invalid 1024",
            "deploy 0.15.0 " + "a" * 40 + " none backend 0",
            "deploy 0.15.0 " + "a" * 40 + " none backend 134217729",
            "deploy 0.15.0 " + "a" * 40 + " none backend 1024 extra",
            "deploy 0.15.0 " + "a" * 40 + " none frontend 0",
            "deploy 0.15.0 " + "a" * 40 + " none frontend 134217129 extra",
            "status extra",
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
