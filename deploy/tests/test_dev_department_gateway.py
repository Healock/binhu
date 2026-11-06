import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]


class DevDepartmentGatewayTests(unittest.TestCase):
    def test_wrapper_accepts_only_fixed_development_seed_contract(self):
        wrapper = (ROOT / "deploy/environments/binhu-dev-application-gateway").read_text()
        self.assertIn("department-seed", wrapper)
        self.assertRegex(wrapper, re.compile(r"dev-department-\[0-9a-f\]\{16\}"))
        self.assertIn("^(measure|apply|verify)$", wrapper)
        self.assertNotIn("production", wrapper.lower())
        self.assertNotIn("staging", wrapper.lower())

    def test_seed_is_fixed_to_dev_platform_database_and_target(self):
        source = (ROOT / "deploy/environments/development_department_gateway.py").read_text(encoding="utf-8")
        self.assertIn('PLATFORM_DATABASE = "Dev_PlatformData"', source)
        self.assertIn('TARGET_NAME = "社区警务队"', source)
        self.assertIn('TARGET_TYPE = "unit"', source)
        self.assertNotIn("sys.argv", source)
        self.assertNotIn("MYSQL_DATABASE", source)

    def test_install_contract_installs_seed_module_and_sudo_rule(self):
        script = (ROOT / "deploy/environments/install-dev-application-gateway.sh").read_text()
        self.assertIn("development_department_gateway.py", script)
        self.assertIn("department-seed *", script)


if __name__ == "__main__":
    unittest.main()
