import unittest

from services.module_registry import (
    MODULE_DEFINITIONS,
    available_module_ids,
    can_access_definition,
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


if __name__ == "__main__":
    unittest.main()
