import unittest
from pathlib import Path

from services.module_registry import (
    MODULE_DEFINITIONS,
    available_module_ids,
    can_access_definition,
    available_mobile_module_ids,
    route_prefixes,
)


class ModuleRegistryTests(unittest.TestCase):
    def test_registered_module_is_available_without_business_code_changes(self):
        custom_module = {
            "module_id": "drinking_report",
            "owner_unit": "community_police",
            "is_public": False,
            "required_permissions": [],
            "enabled": True,
        }
        user = {
            "role": "member",
            "departments": [{"type": "community"}],
            "permissions": [],
        }
        self.assertEqual(available_module_ids(user, [custom_module]), ["drinking_report"])

    def test_unit_ownership_and_permissions_are_both_required(self):
        module = next(item for item in MODULE_DEFINITIONS if item["module_id"] == "community_police")
        outsider = {"role": "member", "departments": [{"type": "internal"}], "permissions": []}
        member = {"role": "member", "departments": [{"type": "community"}], "permissions": []}
        self.assertFalse(can_access_definition(outsider, module))
        self.assertTrue(can_access_definition(member, module))
        protected = {**module, "required_permissions": ["online.raw.view"]}
        self.assertFalse(can_access_definition(member, protected))
        self.assertTrue(can_access_definition({**member, "permissions": ["online.raw.view"]}, protected))

    def test_admin_groups_cross_unit_but_disabled_modules_stay_hidden(self):
        module = next(item for item in MODULE_DEFINITIONS if item["module_id"] == "community_police")
        admin = {"role": "member", "permission_groups": [{"code": "admin"}], "permissions": []}
        self.assertTrue(can_access_definition(admin, module))
        self.assertFalse(can_access_definition(admin, {**module, "enabled": False}))

    def test_empty_registry_fallback_does_not_expose_non_public_modules(self):
        user = {"role": "member", "departments": [], "permissions": []}
        self.assertEqual(available_module_ids(user, []), [])

    def test_registered_routes_are_normalized_and_mobile_visibility_is_applied(self):
        self.assertEqual(route_prefixes('/query, /tasks\n/summary/'), ['/query', '/summary', '/tasks'])
        user = {"role": "admin", "departments": [], "permissions": []}
        hidden = {"module_id": "private", "owner_unit": "common", "is_public": True,
                  "required_permissions": [], "route": "/private", "mobile_visible": False, "enabled": True}
        self.assertNotIn("private", available_mobile_module_ids(user, [hidden]))

    def test_router_gate_assignments_match_registered_module_contract(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        for router_name, dependency in (
            ("spreadsheets_router", "community_police_dep"),
            ("query_router", "community_police_dep"),
            ("workflow_router", "workflow_dep"),
            ("workflow_extended_router", "workflow_dep"),
            ("function_modules_router", "platform_admin_dep"),
            ("users_router", "platform_admin_dep"),
        ):
            self.assertIn(
                f"app.include_router({router_name}, dependencies={dependency})",
                source,
            )


if __name__ == "__main__":
    unittest.main()
