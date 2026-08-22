import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from generate_release_notes import normalize_prs, summarize_body


class ReleaseNotesTests(unittest.TestCase):
    def test_summary_prefers_problem_section_and_removes_sensitive_values(self):
        summary = summarize_body(
            "## 解决了哪些问题\n\n修复 13800138000 和 410422200512270075 的显示问题。\n\n## 验收\n\n其他内容"
        )
        self.assertIn("修复", summary)
        self.assertNotIn("13800138000", summary)
        self.assertNotIn("410422200512270075", summary)
        self.assertNotIn("验收", summary)

    def test_only_commits_in_release_range_are_included(self):
        included = "a" * 40
        excluded = "b" * 40
        prs = normalize_prs(
            [
                {"number": 2, "title": "included", "body": "body", "mergeCommit": {"oid": included}},
                {"number": 1, "title": "excluded", "body": "body", "mergeCommit": {"oid": excluded}},
            ],
            {included},
        )
        self.assertEqual([item["number"] for item in prs], [2])

    def test_summary_keeps_markdown_labels_without_external_destinations(self):
        summary = summarize_body(
            "## Summary\n\n修复[登录页面](https://example.test/private)显示问题。"
        )
        self.assertIn("登录页面", summary)
        self.assertNotIn("https://", summary)


if __name__ == "__main__":
    unittest.main()
