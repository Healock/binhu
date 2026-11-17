"""Rebuild historical online-summary ledgers and reports.

The command is deliberately separate from application startup.  It uses the
existing snapshot rebuild service and requires the normal ``measure ->
 migrate --apply -> verify`` maintenance sequence.  It never contacts an
external source and does not create test data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from typing import Any

from services.report_rebuild import (
    preview_effective_workload_rebuild,
    rebuild_effective_workload_history,
)


def _date(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{name} 必须是 YYYY-MM-DD") from exc


def _parser_types(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))


async def measure(
    *,
    start_date: str | None,
    end_date: str | None,
    parser_types: list[str] | None,
) -> dict[str, Any]:
    return await preview_effective_workload_rebuild(
        start_date=_date(start_date, "开始日期"),
        end_date=_date(end_date, "结束日期"),
        parser_types=_parser_types(parser_types),
    )


async def apply(
    *,
    start_date: str | None,
    end_date: str | None,
    parser_types: list[str] | None,
) -> dict[str, Any]:
    return await rebuild_effective_workload_history(
        start_date=_date(start_date, "开始日期"),
        end_date=_date(end_date, "结束日期"),
        parser_types=_parser_types(parser_types),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    def add_scope_args(command: argparse.ArgumentParser) -> None:
        command.add_argument("--start-date")
        command.add_argument("--end-date")
        command.add_argument("--parser-type", action="append", dest="parser_types")

    measure_parser = sub.add_parser("measure", help="只读列出快照、流水和待回算范围")
    add_scope_args(measure_parser)
    migrate = sub.add_parser("migrate", help="预览或执行历史汇总回算")
    add_scope_args(migrate)
    migrate.add_argument("--apply", action="store_true", help="明确授权写入 daily_report")
    verify_parser = sub.add_parser("verify", help="只读复核回算目标和结果")
    add_scope_args(verify_parser)
    return parser


async def main_async(args: argparse.Namespace) -> None:
    kwargs = {
        "start_date": args.start_date,
        "end_date": args.end_date,
        "parser_types": args.parser_types,
    }
    if args.command == "measure":
        result = await measure(**kwargs)
    elif args.command == "migrate" and args.apply:
        result = await apply(**kwargs)
    elif args.command == "migrate":
        result = await measure(**kwargs)
        result["command"] = "migrate-preview"
    else:
        result = await measure(**kwargs)
        result["command"] = "verify"
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
