"""为全链条和涉警统计迁移新的业务主键。

默认只检查并打印计划；只有显式传入 --apply 才会写数据库。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aiomysql

from config import settings
from services.parsers import get_parser
from services.schema_compat import get_database_column_map, quote_identifier
from services.sync_engine import deduplicate_rows
from services.txdocs_client import TxDocsClient


TARGETS = {
    "全链条": ("身份证号", "电话号码"),
    "涉警统计": ("序号", "日期", "社区"),
}


@dataclass(frozen=True)
class ExistingRow:
    row_id: int
    current_key: str
    data: dict[str, str]


@dataclass(frozen=True)
class RekeyMatch:
    row_id: int
    current_key: str
    target_key: str
    source_data: dict[str, str]


@dataclass
class RekeyPlan:
    parser_type: str
    source_by_key: dict[str, dict[str, str]]
    exact_duplicate_count: int
    existing_count: int
    matches: list[RekeyMatch]
    insert_keys: list[str]

    def summary(self, enabled: bool) -> dict[str, object]:
        return {
            "parser_type": self.parser_type,
            "enabled": enabled,
            "source_rows_after_exact_dedup": len(self.source_by_key),
            "exact_duplicate_rows": self.exact_duplicate_count,
            "existing_rows": self.existing_count,
            "rekey_rows": sum(
                match.current_key != match.target_key for match in self.matches
            ),
            "insert_rows": len(self.insert_keys),
            "expected_final_rows": len(self.source_by_key),
        }


def row_signature(row: dict[str, str], columns: list[str]) -> tuple[str, ...]:
    return tuple(row.get(column, "") for column in columns)


def legacy_signature(
    row: dict[str, str], legacy_columns: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(row.get(column, "") for column in legacy_columns)


def build_rekey_plan(
    parser_type: str,
    source_rows: list[dict],
    existing_rows: list[ExistingRow],
) -> RekeyPlan:
    parser = get_parser(parser_type)
    source_by_key, exact_duplicate_count = deduplicate_rows(parser, source_rows)
    legacy_columns = TARGETS[parser_type]

    source_by_signature = {
        row_signature(row, parser.COLUMNS): key
        for key, row in source_by_key.items()
    }
    source_by_legacy: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for key, row in source_by_key.items():
        source_by_legacy[legacy_signature(row, legacy_columns)].append(key)

    used_keys: set[str] = set()
    matches: list[RekeyMatch] = []
    unmatched_ids: list[int] = []

    for existing in existing_rows:
        target_key = source_by_signature.get(
            row_signature(existing.data, parser.COLUMNS)
        )
        if target_key in used_keys:
            target_key = None

        if target_key is None:
            candidates = [
                key
                for key in source_by_legacy[
                    legacy_signature(existing.data, legacy_columns)
                ]
                if key not in used_keys
            ]
            if len(candidates) == 1:
                target_key = candidates[0]

        if target_key is None:
            unmatched_ids.append(existing.row_id)
            continue

        used_keys.add(target_key)
        matches.append(
            RekeyMatch(
                row_id=existing.row_id,
                current_key=existing.current_key,
                target_key=target_key,
                source_data=source_by_key[target_key],
            )
        )

    if unmatched_ids:
        raise RuntimeError(
            f"{parser_type} 有 {len(unmatched_ids)} 条现有数据无法安全匹配腾讯文档，"
            "迁移已停止"
        )

    insert_keys = sorted(set(source_by_key) - used_keys)
    return RekeyPlan(
        parser_type=parser_type,
        source_by_key=source_by_key,
        exact_duplicate_count=exact_duplicate_count,
        existing_count=len(existing_rows),
        matches=matches,
        insert_keys=insert_keys,
    )


async def load_existing_rows(conn, parser, column_map) -> list[ExistingRow]:
    column_list = ", ".join(
        quote_identifier(column_map[column]) for column in parser.COLUMNS
    )
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT id, _row_key, {column_list} "
            f"FROM {parser.table_name} ORDER BY id"
        )
        rows = await cur.fetchall()

    result = []
    for row in rows:
        data = {
            column: "" if row[index + 2] is None else str(row[index + 2]).strip()
            for index, column in enumerate(parser.COLUMNS)
        }
        result.append(
            ExistingRow(row_id=row[0], current_key=row[1], data=data)
        )
    return result


async def apply_rekey_plan(conn, parser, column_map, plan: RekeyPlan):
    column_list = ", ".join(
        quote_identifier(column_map[column]) for column in parser.COLUMNS
    )
    set_clause = ", ".join(
        f"{quote_identifier(column_map[column])} = %s"
        for column in parser.COLUMNS
    )
    placeholders = ", ".join(["%s"] * (len(parser.COLUMNS) + 1))

    async with conn.cursor() as cur:
        for match in plan.matches:
            if match.current_key == match.target_key:
                continue
            temporary_key = f"__rekey__{parser.table_name}_{match.row_id}"
            await cur.execute(
                f"UPDATE {parser.table_name} SET _row_key = %s WHERE id = %s",
                (temporary_key, match.row_id),
            )

        for match in plan.matches:
            values = [match.target_key] + [
                match.source_data.get(column, "") for column in parser.COLUMNS
            ]
            values.append(match.row_id)
            await cur.execute(
                f"UPDATE {parser.table_name} "
                f"SET _row_key = %s, {set_clause} WHERE id = %s",
                values,
            )

        for key in plan.insert_keys:
            data = plan.source_by_key[key]
            values = [key] + [
                data.get(column, "") for column in parser.COLUMNS
            ]
            await cur.execute(
                f"INSERT INTO {parser.table_name} "
                f"(_row_key, {column_list}) VALUES ({placeholders})",
                values,
            )


async def verify_rekey_plan(conn, parser, column_map, plan: RekeyPlan):
    existing = await load_existing_rows(conn, parser, column_map)
    if len(existing) != len(plan.source_by_key):
        raise RuntimeError(
            f"{plan.parser_type} 迁移后数量不一致："
            f"数据库 {len(existing)}，腾讯文档 {len(plan.source_by_key)}"
        )

    database_by_key = {row.current_key: row.data for row in existing}
    if database_by_key != plan.source_by_key:
        raise RuntimeError(f"{plan.parser_type} 迁移后内容核对不一致")


async def run(apply: bool):
    conn = await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_ONLINE_DATA_DB,
        charset="utf8mb4",
        autocommit=True,
    )
    client = None
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT client_id, access_token, open_id "
                "FROM _config_oauth_tokens ORDER BY id DESC LIMIT 1"
            )
            credentials = await cur.fetchone()
            if not credentials:
                raise RuntimeError("没有可用的腾讯文档凭据")

            placeholders = ",".join(["%s"] * len(TARGETS))
            await cur.execute(
                "SELECT file_id, data_sheet_id, header_row, parser_type, enabled "
                "FROM _config_spreadsheets "
                f"WHERE parser_type IN ({placeholders}) "
                "AND file_id != '' ORDER BY id",
                tuple(TARGETS),
            )
            spreadsheet_rows = await cur.fetchall()

        spreadsheets = {}
        for row in spreadsheet_rows:
            parser_type = row[3]
            if parser_type in spreadsheets:
                raise RuntimeError(f"{parser_type} 配置了多张表，迁移已停止")
            spreadsheets[parser_type] = {
                "file_id": row[0],
                "data_sheet_id": row[1],
                "header_row": row[2],
                "enabled": bool(row[4]),
            }
        missing = set(TARGETS) - set(spreadsheets)
        if missing:
            raise RuntimeError(f"缺少表格配置: {', '.join(sorted(missing))}")

        client = TxDocsClient(credentials[0], credentials[1], credentials[2])
        prepared = []
        for parser_type in TARGETS:
            parser = get_parser(parser_type)
            spreadsheet = spreadsheets[parser_type]
            source_rows = await client.read_all_data(
                spreadsheet["file_id"],
                spreadsheet["data_sheet_id"],
                spreadsheet["header_row"],
                parser.COLUMNS,
            )
            column_map = await get_database_column_map(
                conn, parser.table_name, parser
            )
            existing_rows = await load_existing_rows(
                conn, parser, column_map
            )
            plan = build_rekey_plan(
                parser_type, source_rows, existing_rows
            )
            prepared.append((parser, column_map, plan))
            print(json.dumps(plan.summary(spreadsheet["enabled"])))

        if not apply:
            print(json.dumps({"mode": "dry-run", "database_changed": False}))
            return

        await conn.autocommit(False)
        await conn.begin()
        try:
            for parser, column_map, plan in prepared:
                await apply_rekey_plan(conn, parser, column_map, plan)
                await verify_rekey_plan(conn, parser, column_map, plan)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.autocommit(True)

        print(json.dumps({"mode": "apply", "database_changed": True}))
    finally:
        if client is not None:
            await client.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正执行迁移；不传时只做检查",
    )
    args = parser.parse_args()
    asyncio.run(run(args.apply))


if __name__ == "__main__":
    main()
