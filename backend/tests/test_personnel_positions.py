import unittest

from services.personnel_positions import (
    DEFAULT_SUMMARY_POSITIONS,
    POSITION_CONFIG_KEYS,
    WEEKEND_DUTY_POSITION_CONFIG_KEY,
    filter_person_rows,
    normalize_position,
    parse_position_config,
    serialize_rental_position_config,
    serialize_position_config,
)


class PersonnelPositionTests(unittest.TestCase):
    def test_default_positions_are_group_leader_and_member(self):
        self.assertEqual(
            parse_position_config(None),
            list(DEFAULT_SUMMARY_POSITIONS),
        )
        self.assertIn(
            WEEKEND_DUTY_POSITION_CONFIG_KEY,
            POSITION_CONFIG_KEYS,
        )

    def test_configuration_rejects_empty_or_unknown_positions(self):
        with self.assertRaises(ValueError):
            serialize_position_config("[]")
        with self.assertRaises(ValueError):
            normalize_position("临时岗位")

    def test_configuration_removes_duplicates_and_keeps_order(self):
        self.assertEqual(
            serialize_position_config('["组员", "组长", "组员"]'),
            '["组员", "组长"]',
        )

    def test_rental_configuration_rejects_self_owned_position(self):
        with self.assertRaisesRegex(ValueError, "单独的“自购房”汇总类型"):
            serialize_rental_position_config('["组员", "自购房"]')

    def test_known_unselected_people_are_filtered_but_unknown_people_remain(self):
        rows = [
            ("长板", "组员甲", 1),
            ("长板", "中队长乙", 2),
            ("水秀", "名册外人员", 3),
        ]

        result = filter_person_rows(
            rows,
            name_index=1,
            selected_positions={"组长", "组员"},
            known_positions={
                "组员甲": "组员",
                "中队长乙": "中队长",
            },
        )

        self.assertEqual(result, [rows[0], rows[2]])


if __name__ == "__main__":
    unittest.main()
