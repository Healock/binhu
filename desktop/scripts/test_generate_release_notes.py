import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from generate_release_notes import fallback_user_sections, normalize_prs, parse_release_markdown, summarize_body
from validate_pr_body import REQUIRED_HEADINGS, validate_body


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

    def test_summary_recovers_literal_newlines_from_api_body(self):
        summary = summarize_body(
            "## 修改范围\\n- 修复更新弹窗\\n- 生成更新日志\\n\\n## 验收建议\\n- 升级后检查弹窗"
        )
        self.assertEqual(summary, "修复更新弹窗 生成更新日志")

    def test_release_level_sections_are_parsed_and_sensitive_values_are_removed(self):
        sections = parse_release_markdown(
            "1. 任务编辑与照片回写可靠性\n"
            "- 修复 13800138000 的回写问题。\n"
            "- 提升批量处理稳定性。"
        )
        self.assertEqual(sections[0]["title"], "任务编辑与照片回写可靠性")
        self.assertEqual(len(sections[0]["items"]), 2)
        self.assertNotIn("13800138000", " ".join(sections[0]["items"]))

    def test_markdown_comments_are_not_published(self):
        sections = parse_release_markdown(
            "<!--\n"
            "1. 示例主题\n"
            "- 示例内容\n"
            "-->"
        )
        self.assertEqual(sections, [])

    def test_release_notes_fallback_keeps_uncategorized_prs_publishable(self):
        prs = [
            {"number": 3, "title": "构建", "summary": "构建摘要"},
        ]
        sections = fallback_user_sections(prs)
        self.assertEqual([section["title"] for section in sections], ["其他更新"])
        self.assertIn("#3 构建：构建摘要", sections[0]["items"])

    def test_pr_body_rejects_literal_newlines_and_missing_template_sections(self):
        errors = validate_body("## 修改范围\\n- bad")
        self.assertTrue(any("字面量" in error for error in errors))
        self.assertTrue(any("PR 摘要" in error for error in errors))

    def test_pr_body_allows_literal_newline_term_inside_valid_template(self):
        body = "\n\n".join(REQUIRED_HEADINGS) + "\n\n说明文字可以讨论 `\\n` 本身。"
        self.assertEqual(validate_body(body), [])


if __name__ == "__main__":
    unittest.main()
