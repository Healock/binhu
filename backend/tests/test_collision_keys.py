import hashlib
import os
import sys
import types
import unittest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

try:
    import aiomysql  # noqa: F401
except ModuleNotFoundError:
    aiomysql_stub = types.ModuleType("aiomysql")
    aiomysql_stub.Pool = object
    sys.modules["aiomysql"] = aiomysql_stub

from migrations.rekey_collision_tables import ExistingRow, build_rekey_plan
from services.parsers.base import BaseParser
from services.parsers.fullchain import FullChainParser
from services.parsers.police_stats import PoliceStatsParser
from services.sync_engine import deduplicate_rows


def make_row(parser, **values):
    return {column: values.get(column, "") for column in parser.COLUMNS}


def legacy_key(*values):
    return hashlib.md5("|".join(values).encode()).hexdigest()


class UnsafeParser(BaseParser):
    parser_type = "unsafe"
    table_name = "t_unsafe"
    COLUMNS = ["编号", "内容"]

    def get_business_key(self):
        return ["编号"]


class CollisionKeyTests(unittest.TestCase):
    def test_fullchain_dispatch_date_distinguishes_batches(self):
        parser = FullChainParser()
        first = make_row(
            parser,
            身份证号="person-1",
            电话号码="phone-1",
            下发日期="2026-07-01",
        )
        second = {**first, "下发日期": "2026-07-27"}

        self.assertNotEqual(
            parser.make_row_key(first),
            parser.make_row_key(second),
        )

    def test_police_summary_distinguishes_incidents(self):
        parser = PoliceStatsParser()
        first = make_row(
            parser,
            序号="1",
            日期="7.27",
            社区="社区甲",
            简要警情及处理结果="记录一",
        )
        second = {**first, "简要警情及处理结果": "记录二"}

        self.assertNotEqual(
            parser.make_row_key(first),
            parser.make_row_key(second),
        )

    def test_exact_duplicate_is_allowed(self):
        parser = PoliceStatsParser()
        row = make_row(
            parser,
            序号="1",
            日期="7.27",
            社区="社区甲",
            简要警情及处理结果="相同记录",
        )

        online, duplicate_count = deduplicate_rows(parser, [row, dict(row)])

        self.assertEqual(len(online), 1)
        self.assertEqual(duplicate_count, 1)

    def test_different_rows_with_same_key_stop_sync(self):
        parser = UnsafeParser()

        with self.assertRaisesRegex(ValueError, "未覆盖任何冲突行"):
            deduplicate_rows(
                parser,
                [
                    {"编号": "1", "内容": "第一条"},
                    {"编号": "1", "内容": "第二条"},
                ],
            )

    def test_fullchain_plan_rekeys_existing_and_inserts_missing_row(self):
        parser = FullChainParser()
        first = make_row(
            parser,
            身份证号="person-1",
            电话号码="phone-1",
            下发日期="2026-07-01",
            现住址="地址一",
        )
        second = {**first, "下发日期": "2026-07-27", "现住址": "地址二"}
        existing = ExistingRow(
            row_id=10,
            current_key=legacy_key("person-1", "phone-1"),
            data=second,
        )

        plan = build_rekey_plan("全链条", [first, second], [existing])

        self.assertEqual(len(plan.source_by_key), 2)
        self.assertEqual(len(plan.matches), 1)
        self.assertEqual(len(plan.insert_keys), 1)
        self.assertEqual(plan.existing_count, 1)
        self.assertNotEqual(
            plan.matches[0].current_key,
            plan.matches[0].target_key,
        )

    def test_police_plan_collapses_only_exact_duplicate(self):
        parser = PoliceStatsParser()
        first = make_row(
            parser,
            序号="",
            日期="",
            社区="社区甲",
            简要警情及处理结果="记录一",
        )
        second = {**first, "简要警情及处理结果": "记录二"}
        existing = ExistingRow(
            row_id=20,
            current_key=legacy_key("", "", "社区甲"),
            data=second,
        )

        plan = build_rekey_plan(
            "涉警统计",
            [first, dict(first), second],
            [existing],
        )

        self.assertEqual(len(plan.source_by_key), 2)
        self.assertEqual(plan.exact_duplicate_count, 1)
        self.assertEqual(len(plan.matches), 1)
        self.assertEqual(len(plan.insert_keys), 1)

    def test_unmatched_existing_row_stops_migration(self):
        parser = FullChainParser()
        source = make_row(
            parser,
            身份证号="source-person",
            电话号码="source-phone",
            下发日期="2026-07-27",
        )
        existing = ExistingRow(
            row_id=30,
            current_key="old-key",
            data=make_row(
                parser,
                身份证号="missing-person",
                电话号码="missing-phone",
                下发日期="2026-07-01",
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "迁移已停止"):
            build_rekey_plan("全链条", [source], [existing])


if __name__ == "__main__":
    unittest.main()
