from pathlib import Path
import re
import unittest


WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "desktop-release.yml"


class DesktopReleaseWorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_publish_job_has_bounded_timeout_for_server_pull(self) -> None:
        publish_job = self.workflow.split("\n  publish:\n", 1)[1]
        timeout = re.search(r"^    timeout-minutes:\s*(\d+)\s*$", publish_job, re.MULTILINE)
        self.assertIsNotNone(timeout)
        self.assertGreaterEqual(int(timeout.group(1)), 120)

    def test_publish_creates_asset_before_fixed_gateway_pull(self) -> None:
        self.assertIn("ALIYUN_OSS_UPLOAD_ENDPOINT: binhu-update.oss-accelerate.aliyuncs.com", self.workflow)
        self.assertIn("ALIYUN_OSS_STANDARD_ENDPOINT: binhu-update.oss-cn-shanghai.aliyuncs.com", self.workflow)
        self.assertLess(
            self.workflow.index("Compare standard and accelerated OSS endpoints"),
            self.workflow.index("Upload transfer bundle to private Aliyun OSS"),
        )
        self.assertIn("aliyun_oss_transfer.py verify", self.workflow)
        self.assertIn('"average_bytes_per_second"', self.workflow)
        self.assertLess(
            self.workflow.index("Upload transfer bundle to private Aliyun OSS"),
            self.workflow.index("Publish through fixed SSH gateway"),
        )
        start = self.workflow.index("      - name: Publish through fixed SSH gateway")
        publish_step = self.workflow[start:]
        self.assertIn("pull-oss-object", publish_step)
        self.assertNotIn("< \"$bundle\"", publish_step)
        self.assertNotIn("Create temporary GitHub transfer release", self.workflow)
        self.assertIn("ALIYUN_OSS_ACCESS_KEY_ID", self.workflow)
        self.assertIn("ALIYUN_OSS_BUCKET", self.workflow)
        self.assertIn("ALIYUN_OSS_SERVER_ENDPOINT", self.workflow)
        self.assertIn("aliyun_oss_transfer.py presign", self.workflow)

    def test_publish_ssh_has_bounded_connect_retries_and_keepalive(self) -> None:
        start = self.workflow.index("      - name: Publish through fixed SSH gateway")
        publish_step = self.workflow[start:]
        self.assertIn("-o ConnectTimeout=30", publish_step)
        self.assertIn("-o ConnectionAttempts=3", publish_step)
        self.assertIn("-o ServerAliveInterval=30", publish_step)
        self.assertIn("-o ServerAliveCountMax=20", publish_step)
        self.assertIn('echo "bundle_bytes=$size"', self.workflow)

    def test_transfer_object_is_deleted_only_after_release_creation(self) -> None:
        release = self.workflow.index("      - name: Create audit GitHub Release")
        cleanup = self.workflow.index("      - name: Delete transfer object after successful release")
        self.assertLess(release, cleanup)
        cleanup_step = self.workflow[cleanup:]
        self.assertIn("aliyun_oss_transfer.py delete", cleanup_step)
        self.assertIn("steps.transfer.outputs.object_key", cleanup_step)
        self.assertIn("ALIYUN_OSS_ACCESS_KEY_ID", cleanup_step)


if __name__ == "__main__":
    unittest.main()
