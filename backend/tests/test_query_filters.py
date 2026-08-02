import json
import os
import unittest


os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.query import _append_grid_filters, _grid_filter_condition


class QueryFilterHelpersTest(unittest.TestCase):
    def test_single_filter_conditions(self):
        self.assertEqual(
            _grid_filter_condition("field", {"type": "blank"}),
            ("COALESCE(field, '') = ''", []),
        )
        self.assertEqual(
            _grid_filter_condition(
                "field", {"type": "notStartsWith", "filter": "长板"}
            ),
            ("field NOT LIKE %s", ["长板%"]),
        )
        self.assertEqual(
            _grid_filter_condition(
                "field", {"type": "greaterThanOrEqual", "filter": "10"}
            ),
            ("CAST(field AS DECIMAL(30, 6)) >= %s", ["10"]),
        )

    def test_compound_filters_preserve_and_or_semantics(self):
        where_parts = []
        params = []
        _append_grid_filters(
            where_parts,
            params,
            json.dumps(
                {
                    "数量": {
                        "operator": "and",
                        "conditions": [
                            {"type": "greaterThanOrEqual", "filter": "10"},
                            {"type": "lessThanOrEqual", "filter": "20"},
                        ],
                    },
                    "姓名": {
                        "operator": "or",
                        "conditions": [
                            {"type": "startsWith", "filter": "张"},
                            {"type": "endsWith", "filter": "明"},
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            ["数量", "姓名"],
            lambda column: f"json_{column}",
        )

        self.assertEqual(
            where_parts,
            [
                "(CAST(json_数量 AS DECIMAL(30, 6)) >= %s AND "
                "CAST(json_数量 AS DECIMAL(30, 6)) <= %s)",
                "(json_姓名 LIKE %s OR json_姓名 LIKE %s)",
            ],
        )
        self.assertEqual(params, ["10", "20", "张%", "%明"])

    def test_invalid_json_and_unknown_columns_are_ignored(self):
        where_parts = []
        params = []
        _append_grid_filters(
            where_parts,
            params,
            "not-json",
            ["社区"],
            lambda column: column,
        )
        _append_grid_filters(
            where_parts,
            params,
            json.dumps({"不存在": {"type": "equals", "filter": "值"}}),
            ["社区"],
            lambda column: column,
        )
        self.assertEqual(where_parts, [])
        self.assertEqual(params, [])


if __name__ == "__main__":
    unittest.main()
