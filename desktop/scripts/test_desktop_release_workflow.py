from pathlib import Path
import re
import unittest


WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "desktop-release.yml"


class DesktopReleaseWorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_publish_job_allows_slow_fixed_gateway_upload(self) -> None:
        publish_job = self.workflow.split("\n  publish:\n", 1)[1]
        timeout = re.search(r"^    timeout-minutes:\s*(\d+)\s*$", publish_job, re.MULTILINE)
        self.assertIsNotNone(timeout)
        self.assertGreaterEqual(int(timeout.group(1)), 360)

    def test_publish_ssh_has_bounded_connect_retries_and_keepalive(self) -> None:
        start = self.workflow.index("      - name: Publish through fixed SSH gateway")
        publish_step = self.workflow[start:]
        self.assertIn("-o ConnectTimeout=30", publish_step)
        self.assertIn("-o ConnectionAttempts=3", publish_step)
        self.assertIn("-o ServerAliveInterval=30", publish_step)
        self.assertIn("-o ServerAliveCountMax=20", publish_step)
        self.assertIn('echo "bundle_bytes=$size"', publish_step)


if __name__ == "__main__":
    unittest.main()
