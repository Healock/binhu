from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.report_builders import BUILDERS
from services.report_rebuild import (
    rebuild_effective_workload_history,
    preview_effective_workload_rebuild,
)


def test_rebuild_rejects_invalid_date_before_touching_database():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        asyncio.run(preview_effective_workload_rebuild(start_date="2026-8-5"))


def test_rebuild_runs_snapshots_in_date_order_and_forces_ledger_reset():
    preview = {
        "targets": [
            {
                "report_date": "2026-08-04",
                "parser_type": "全链条",
                "snapshot_table": "2026-08-04_snapshot_fullChain",
            },
            {
                "report_date": "2026-08-03",
                "parser_type": "全链条",
                "snapshot_table": "2026-08-03_snapshot_fullChain",
            },
        ],
        "date_count": 2,
        "snapshot_count": 2,
        "existing_ledger_rows": 20,
        "existing_effective_workload": 8,
    }
    builder = BUILDERS["全链条"]
    build = AsyncMock(side_effect=[
        {"date": "2026-08-03", "ledger_rows": 10},
        {"date": "2026-08-04", "ledger_rows": 10},
    ])
    summary = AsyncMock(side_effect=[
        {"date": "2026-08-03", "implemented": True},
        {"date": "2026-08-04", "implemented": True},
    ])

    with patch(
        "services.report_rebuild.preview_effective_workload_rebuild",
        new=AsyncMock(return_value=preview),
    ), patch.object(builder, "build", new=build), patch(
        "services.report_rebuild.build_summary", new=summary,
    ):
        result = asyncio.run(rebuild_effective_workload_history())

    assert [item["report_date"] for item in result["rebuilt"]] == [
        "2026-08-03", "2026-08-04",
    ]
    assert [call.args[0] for call in build.await_args_list] == [
        "2026-08-03", "2026-08-04",
    ]
    assert all(
        call.kwargs == {
            "generation_method": "workload_backfill",
            "reset_ledger": True,
        }
        for call in build.await_args_list
    )
    assert all(
        call.kwargs == {"generation_method": "workload_backfill"}
        for call in summary.await_args_list
    )
