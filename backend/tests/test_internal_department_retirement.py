import os
import unittest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from migrations.internal_department_retirement import (
    NAMED_TARGETS,
    _target_for_member,
    build_parser,
)


class InternalDepartmentRetirementTests(unittest.TestCase):
    def test_confirmed_named_person_maps_only_with_expected_position(self):
        self.assertEqual(
            _target_for_member({"name": "张航", "position": "所队领导", "area_id": None}),
            "社区警务队",
        )
        self.assertIsNone(
            _target_for_member({"name": "张航", "position": "组员", "area_id": None})
        )

    def test_self_owned_person_uses_area_mapping_marker(self):
        self.assertEqual(
            _target_for_member({"name": "未命名", "position": "自购房", "area_id": 3}),
            "__area__:3",
        )
        self.assertIsNone(
            _target_for_member({"name": "未命名", "position": "自购房", "area_id": None})
        )

    def test_mapping_is_explicit_and_parser_exposes_apply_gates(self):
        self.assertNotIn("金润逸", NAMED_TARGETS)
        self.assertEqual(
            _target_for_member({"name": "金润逸", "position": "岗位待定", "area_id": None}),
            "__unassigned__",
        )
        args = build_parser().parse_args([
            "migrate", "--apply", "--run-id", "run-20261015",
            "--backup-manifest", "backup.json", "--expect-internal-members", "12",
            "--confirm-remove-internal",
        ])
        self.assertTrue(args.apply)
        self.assertTrue(args.confirm_remove_internal)
        self.assertEqual(args.expect_internal_members, 12)

    def test_direct_comprehensive_command_member_has_no_position(self):
        self.assertEqual(
            _target_for_member({"name": "高泽鑫", "position": "", "area_id": None}),
            "综合指挥室",
        )
        self.assertIsNone(
            _target_for_member({"name": "高泽鑫", "position": "科员", "area_id": None})
        )


if __name__ == "__main__":
    unittest.main()
