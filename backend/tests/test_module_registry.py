import unittest
from pathlib import Path

from services.module_registry import (
    MODULE_DEFINITIONS,
    _department_ancestors,
    available_module_ids,
    can_access_definition,
    available_mobile_module_ids,
    route_prefixes,
)


class ModuleRegistryTests(unittest.TestCase):
    def test_registered_module_uses_department_ids_without_business_code_changes(self):
        custom_module = {
            "module_id": "drinking_report",
            "department_ids": [10],
            "is_public": False,
            "required_permissions": [],
            "enabled": True,
        }
        user = {
            "role": "member",
            "departments": [{"id": 11, "parent_id": 10, "type": "internal"}],
            "permissions": [],
        }
        self.assertEqual(available_module_ids(user, [custom_module]), ["drinking_report"])

    def test_department_tree_expands_all_ancestors(self):
        self.assertEqual(_department_ancestors([4], {4: 3, 3: 2, 2: 1, 1: None}), {1, 2, 3, 4})

    def test_department_id_not_department_type_controls_access(self):
        module = next(item for item in MODULE_DEFINITIONS if item["module_id"] == "community_police")
        outsider = {"role": "member", "departments": [{"id": 9, "parent_id": None, "type": "community"}], "permissions": []}
        member = {"role": "member", "departments": [{"id": 2, "parent_id": 1, "type": "unit"}], "permissions": []}
        bound = {**module, "department_ids": [1]}
        self.assertFalse(can_access_definition(outsider, bound))
        self.assertTrue(can_access_definition(member, bound))
        protected = {**bound, "required_permissions": ["online.raw.view"]}
        self.assertFalse(can_access_definition(member, protected))
        self.assertTrue(can_access_definition({**member, "permissions": ["online.raw.view"]}, protected))

    def test_admin_crosses_departments_but_disabled_stays_hidden(self):
        module = {"module_id": "private", "department_ids": [1], "is_public": False, "required_permissions": [], "enabled": True}
        admin = {"role": "member", "permission_groups": [{"code": "admin"}], "permissions": []}
        self.assertTrue(can_access_definition(admin, module))
        self.assertFalse(can_access_definition(admin, {**module, "enabled": False}))

    def test_empty_registry_does_not_expose_non_public_modules(self):
        user = {"role": "member", "departments": [], "permissions": []}
        self.assertEqual(available_module_ids(user, []), [])

    def test_public_and_mobile_flags_remain_effective(self):
        self.assertEqual(route_prefixes('/query, /tasks\n/summary/'), ['/query', '/summary', '/tasks'])
        user = {"role": "member", "departments": [], "permissions": []}
        hidden = {"module_id": "private", "department_ids": [], "is_public": True,
                  "required_permissions": [], "route": "/private", "mobile_visible": False, "enabled": True}
        self.assertNotIn("private", available_mobile_module_ids(user, [hidden]))

    def test_router_gate_assignments_match_registered_module_contract(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("app.include_router(function_modules_router, dependencies=auth_dep)", source)
        for router_name, dependency in (
            ("spreadsheets_router", "community_police_dep"),
            ("query_router", "community_police_dep"),
            ("workflow_router", "workflow_dep"),
            ("users_router", "platform_admin_dep"),
        ):
            self.assertIn(f"app.include_router({router_name}, dependencies={dependency})", source)


if __name__ == "__main__":
    unittest.main()
