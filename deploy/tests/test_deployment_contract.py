from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DeploymentContractTests(unittest.TestCase):
    def test_production_workflow_only_promotes_accepted_staging_candidates(self) -> None:
        workflow = (ROOT / ".github/workflows/deploy-production.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("workflow_call:", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotRegex(workflow, re.compile(r"(?m)^\s+push:\s*$"))
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("group: production-deploy", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("environment: production", workflow)
        self.assertIn("STAGING_RUN_ID", workflow)
        self.assertIn("staging-app-[0-9a-f]{16}", workflow)
        self.assertIn("sha256sum docker-compose.yml", workflow)
        self.assertIn(
            'git merge-base --is-ancestor "$release_commit" origin/main', workflow
        )
        self.assertIn('"binhu-deploy@$DEPLOY_HOST"', workflow)
        self.assertNotIn('"root@$DEPLOY_HOST"', workflow)
        self.assertIn("-o BatchMode=yes", workflow)
        self.assertIn("-o StrictHostKeyChecking=yes", workflow)
        self.assertIn("-o PasswordAuthentication=no", workflow)
        self.assertIn(
            '"promote $EXPECTED_VERSION $STAGING_RUN_ID $RELEASE_COMMIT $PRODUCTION_COMPOSE_SHA256 $PRODUCTION_COMPOSE_SIZE"',
            workflow,
        )
        self.assertIn("< docker-compose.yml", workflow)
        self.assertNotIn("backup_scope:", workflow)
        self.assertNotIn("release_scope:", workflow)
        self.assertNotIn("build_release_bundle.py", workflow)
        self.assertNotIn("docker compose", workflow)
        self.assertNotIn("docker build", workflow)

        action_refs = re.findall(r"uses:\s+[^@\s]+@([^\s#]+)", workflow)
        self.assertGreaterEqual(len(action_refs), 1)
        for action_ref in action_refs:
            self.assertRegex(action_ref, r"^[0-9a-f]{40}$")

    def test_emergency_hotfix_uses_exact_main_ci_candidate_and_fixed_full_backup(self) -> None:
        workflow = (ROOT / ".github/workflows/deploy-hotfix.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("hotfix_id:", workflow)
        self.assertIn("artifact_run_id:", workflow)
        self.assertIn("run.get('event') != 'push'", workflow)
        self.assertIn("run.get('head_sha') != sys.argv[3]", workflow)
        self.assertIn("binhu-hotfix-candidate-${{ inputs.ref }}", workflow)
        self.assertIn("'backup_scope': 'all'", workflow)
        self.assertIn('"hotfix $EXPECTED_VERSION $commit all backend $size $HOTFIX_ID $bundle_sha256"', workflow)
        self.assertIn('"binhu-deploy@$DEPLOY_HOST"', workflow)
        self.assertIn("docker image load --input \"$work_dir/image.tar.gz\"", (ROOT / "deploy/binhu-deploy").read_text(encoding="utf-8"))
        self.assertIn("docker image load --input \"$promotion_candidate/image.tar\"", (ROOT / "deploy/binhu-deploy").read_text(encoding="utf-8"))

    def test_production_compose_is_no_build(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotRegex(compose, re.compile(r"(?m)^\s+build\s*:"))
        self.assertGreaterEqual(compose.count("image: binhu-backend"), 3)

    def test_ci_never_references_production_secrets_or_gateway(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request:", workflow)
        self.assertIn("push:", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("binhu-deploy@", workflow)

    def test_client_release_can_read_merged_pull_requests_for_notes(self) -> None:
        workflow = (ROOT / ".github/workflows/desktop-release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("permissions:\n  contents: write\n  pull-requests: read", workflow)
        self.assertIn("gh pr list", workflow)
        self.assertIn("validate_release_versions.py", workflow)

    def test_client_release_frontend_build_has_explicit_node_heap(self) -> None:
        workflow = (ROOT / ".github/workflows/desktop-release.yml").read_text(
            encoding="utf-8"
        )
        frontend_job = workflow.split("\n  frontend:\n", 1)[1].split(
            "\n  baseline:\n", 1
        )[0]
        self.assertIn("NODE_OPTIONS: --max-old-space-size=8192", frontend_job)
        self.assertIn("npm test && npm run build -- --mode desktop", frontend_job)

    def test_client_release_pins_webview2_bootstrapper_file_and_hash(self) -> None:
        workflow = (ROOT / ".github/workflows/desktop-release.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("go.microsoft.com/fwlink", workflow)
        self.assertRegex(
            workflow,
            re.compile(
                r"WEBVIEW2_BOOTSTRAPPER_URL: https://msedge\.sf\.dl\.delivery\.mp\.microsoft\.com/filestreamingservice/files/[0-9a-f-]+/MicrosoftEdgeWebview2Setup\.exe"
            ),
        )
        self.assertRegex(
            workflow,
            re.compile(r"WEBVIEW2_BOOTSTRAPPER_SHA256: [0-9a-f]{64}"),
        )
        self.assertIn("WebView2 bootstrapper hash mismatch", workflow)

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
        fail_function = script.split("fail() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("declare -F rollback_after_error", fail_function)
        self.assertIn('[[ "${switched:-0}" -eq 1 ]]', fail_function)
        self.assertIn("rollback_after_error 1", fail_function)
        self.assertIn("archive exceeds extraction limits", script)
        self.assertIn("BINHU_DEPLOY_PUBLIC_URL", script)
        self.assertIn("curl --http1.1 --fail", script)
        self.assertIn('[[ "$work_dir" == "$state_dir/work/"* ]]', script)
        self.assertIn('head -c "$expected_bundle_size"', script)
        self.assertIn('[[ "$bundle_size" == "$expected_bundle_size" ]]', script)
        self.assertIn('release_scope="full"', script)
        self.assertIn('RELEASE_SCOPE=%s', script)
        self.assertIn('docker image load --input "$promotion_candidate/image.tar"', script)
        self.assertIn('docker image load --input "$work_dir/image.tar.gz"', script)
        self.assertIn('docker image inspect "$promotion_image_id"', script)
        self.assertIn('loaded prebuilt exact-main-CI hotfix image', script)
        self.assertIn('loaded exact Staging-accepted image', script)
        self.assertIn('docker compose up -d --no-build --no-deps --force-recreate ops-agent backend', script)
        self.assertIn('docker compose up -d --no-build --no-deps --force-recreate ops-agent backend event-relay diagnostic-worker', script)
        self.assertIn('promotion image archive digest mismatch', script)
        self.assertIn('hotfix bundle digest mismatch', script)
        self.assertIn('hotfix candidate manifest digest mismatch', script)
        self.assertIn('hotfix requires all/backend scope', script)
        self.assertNotIn("docker build", script)
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
            "IMAGE_LOAD",
            "PROGRAM_BACKUP",
            "DATABASE_BACKUP",
            "SWITCH_FILES",
            "CONTAINER_RECREATE",
            "HEALTH_CHECKS",
        ):
            self.assertIn(f"DEPLOY_TIMING_{timing_name}_SECONDS", script)
        self.assertIn('if [[ "$status" == "success" ]]', script)
        self.assertIn('rm -rf -- "$project_dir/backend"', script)
        self.assertIn('tar -czf "$program_backup" -C "$project_dir" \\', script)
        self.assertIn('switched=1', script)
        self.assertNotIn('BINHU_DEPLOY_MAX_BUNDLE_BYTES + 1', script)

    def test_hotfix_recreates_only_backend_and_ops_agent_on_success_and_rollback(self) -> None:
        script = (ROOT / "deploy/binhu-deploy").read_text(encoding="utf-8")
        rollback = script.split("rollback_after_error() {", 1)[1].split("\n}", 1)[0]
        recreate = script.split('container_recreate_started="$(date +%s)"', 1)[1].split(
            'timing_container_recreate=$(( $(date +%s) - container_recreate_started ))', 1
        )[0]
        hotfix_only = re.compile(
            r'if \[\[ "\$release_kind" == "hotfix" \]\]; then\s+'
            r'docker compose up -d --no-build --no-deps --force-recreate ops-agent backend\s+'
            r'else\s+'
            r'docker compose up -d --no-build --no-deps --force-recreate '
            r'ops-agent backend event-relay diagnostic-worker\s+fi',
        )
        self.assertRegex(recreate, hotfix_only)
        self.assertRegex(rollback, hotfix_only)

    def test_deployer_installs_root_only_promotion_verifier_and_restricts_sudo(self) -> None:
        installer = (ROOT / "deploy/install-server-deployer.sh").read_text(encoding="utf-8")
        self.assertIn("production_promotion.py", installer)
        self.assertIn("artifact.py", installer)
        self.assertIn("image.py", installer)
        self.assertIn("binhu-deploy promote *", installer)
        self.assertEqual(installer.count("binhu-deploy hotfix *"), 1)
        self.assertNotRegex(installer, re.compile(r"NOPASSWD: /usr/local/sbin/binhu-deploy \*\n"))

    def test_assignment_projection_backfill_does_not_block_api_bootstrap(self) -> None:
        database = (ROOT / "backend/database.py").read_text(encoding="utf-8")
        self.assertIn("ensure_assignment_projection_backfill_schema(cur)", database)
        self.assertNotIn("await run_assignment_projection_backfill(conn)", database)

    def test_local_source_migration_is_production_only_for_immutable_staging_snapshots(self) -> None:
        database = (ROOT / "backend/database.py").read_text(encoding="utf-8")
        self.assertIn('if settings.APP_ENVIRONMENT == "production":', database)
        self.assertIn('await run_local_source_migration(conn)', database)

    @unittest.skipIf(os.name == "nt", "受限部署网关测试需要 Linux bash")
    def test_gateway_rejects_any_command_outside_fixed_grammar(self) -> None:
        gateway = ROOT / "deploy/binhu-deploy-gateway"
        for command in (
            "",
            "bash",
            "deploy 0.15.0 deadbeef none",
            "hotfix 0.15.0 " + "a" * 40 + " online backend 1024 HF-20261027-1 " + "b" * 64,
            "hotfix 0.15.0 " + "a" * 40 + " all full 1024 HF-20261027-1 " + "b" * 64,
            "promote 1.2.3 staging-app-" + "a" * 16,
            "promote 1.2.3 staging-app-" + "a" * 16 + " " + "a" * 40 + " f" * 64 + " 4",
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
