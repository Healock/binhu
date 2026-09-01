import os
import unittest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.online_source import assignment_projection_fields


class AssignmentProjectionTests(unittest.TestCase):
    def test_ready_projection_contains_display_and_sort_fields(self):
        source, address, sort_key, ready = assignment_projection_fields(
            "疑似未注销模型三",
            {"姓名": "甲", "地址": "  江苏省  苏州市  ", "核查结果": ""},
            community="社区一",
            source_count=1,
            conflict=False,
            task_state_value="unchecked",
        )
        self.assertTrue(source)
        self.assertEqual(address, "江苏省  苏州市")
        self.assertEqual(sort_key, "江苏省苏州市")
        self.assertEqual(ready, 1)

    def test_completed_or_assigned_projection_is_not_queue_ready(self):
        for values, state in (
            ({"核查人": "乙"}, "unchecked"),
            ({}, "completed"),
        ):
            result = assignment_projection_fields(
                "疑似未注销模型三",
                values,
                community="社区一",
                source_count=1,
                conflict=False,
                task_state_value=state,
            )
            self.assertEqual(result[-1], 0)

    def test_duplicate_source_is_not_queue_ready(self):
        result = assignment_projection_fields(
            "疑似未注销模型三",
            {},
            community="社区一",
            source_count=2,
            conflict=True,
            task_state_value="unchecked",
        )
        self.assertEqual(result[-1], 0)


if __name__ == "__main__":
    unittest.main()
