from unittest.mock import AsyncMock, patch

import pytest

from routers import stats


def _overlay_payload():
    return {
        "available": True,
        "total_tasks": 2,
        "carryover_tasks": 0,
        "new_tasks": 2,
        "changed_tasks": 1,
        "pending_tasks": 1,
        "completed_tasks": 1,
        "last_success_at": "2026-09-20T01:00:00Z",
        "communities": {
            "社区一": {
                "total": 2,
                "pending": 1,
                "new": 2,
                "changed": 1,
                "unchecked": 1,
                "checked": 0,
                "completed": 1,
                "carryover": 0,
                "assignees": {
                    "测试人员甲": {
                        "total": 1,
                        "pending": 1,
                        "new": 1,
                        "changed": 1,
                        "unchecked": 1,
                        "checked": 0,
                        "completed": 0,
                        "carryover": 0,
                        "sources": ["txdocs_readonly"],
                    },
                    "测试人员乙": {
                        "total": 1,
                        "pending": 0,
                        "new": 1,
                        "changed": 0,
                        "unchecked": 0,
                        "checked": 0,
                        "completed": 1,
                        "carryover": 0,
                        "sources": ["txdocs_readonly"],
                    },
                },
            },
        },
    }


@pytest.mark.asyncio
async def test_external_overlay_merges_counts_and_recalculates_rates():
    result = {
        "exists": True,
        "community": {
            "columns": [
                "社区", "数据总数", "未核查", "已核查", "已完成",
                "核查完成率", "无法见底数", "核查见底率",
            ],
            "data": [{
                "社区": "社区一",
                "数据总数": 8,
                "未核查": 2,
                "已核查": 3,
                "已完成": 3,
                "核查完成率": 0.38,
                "无法见底数": 1,
                "核查见底率": 0.75,
            }],
        },
        "inspector": {"columns": [], "data": []},
    }

    with patch.object(
        stats, "get_txdocs_business_overlay", new=AsyncMock(return_value=_overlay_payload())
    ):
        actual = await stats._overlay_external_report(
            result,
            start_date="2026-09-20",
            end_date="2026-09-20",
            parser_type="全链条",
            communities=["社区一"],
        )

    row = actual["community"]["data"][0]
    assert row["数据总数"] == 10
    assert row["未核查"] == 3
    assert row["已核查"] == 3
    assert row["已完成"] == 4
    assert row["核查完成率"] == 0.4
    # Existing unable-to-verify data is retained; external rows contribute 0.
    assert row["无法见底数"] == 1
    assert row["核查见底率"] == 0.8
    assert actual["external_overlay"]["available"] is True


@pytest.mark.asyncio
async def test_external_overlay_uses_checker_names_and_merges_same_name():
    result = {
        "exists": True,
        "community": {"columns": ["社区", "数据总数"], "data": []},
        "inspector": {
            "columns": ["社区", "姓名", "数据总数", "未核查", "已核查", "已完成"],
            "data": [{
                "社区": "社区一", "姓名": "测试人员甲", "数据总数": 3,
                "未核查": 1, "已核查": 1, "已完成": 1,
            }],
        },
    }

    with patch.object(
        stats, "get_txdocs_business_overlay", new=AsyncMock(return_value=_overlay_payload())
    ):
        actual = await stats._overlay_external_report(
            result,
            start_date="2026-09-20",
            end_date="2026-09-20",
            parser_type="全链条",
            communities=None,
        )

    rows = {row["姓名"]: row for row in actual["inspector"]["data"]}
    assert "外部腾讯表（只读）" not in rows
    assert rows["测试人员甲"]["数据总数"] == 4
    assert rows["测试人员甲"]["未核查"] == 2
    assert rows["测试人员乙"]["数据总数"] == 1
    assert rows["测试人员乙"]["已完成"] == 1
    assert "数据来源" not in actual["inspector"]["columns"]
    assert all("数据来源" not in row for row in rows.values())


@pytest.mark.asyncio
async def test_model_three_external_overlay_is_included_in_checker_statistics():
    result = {
        "exists": True,
        "community": {"columns": ["社区", "数据总数"], "data": []},
        "inspector": {
            "columns": ["社区", "姓名", "数据总数", "未核查", "已核查", "已完成"],
            "data": [],
        },
    }
    read_overlay = AsyncMock(return_value=_overlay_payload())

    with patch.object(stats, "get_txdocs_business_overlay", new=read_overlay):
        actual = await stats._overlay_external_report(
            result,
            start_date="2026-09-20",
            end_date="2026-09-20",
            parser_type="疑似未注销模型三",
            communities=None,
        )

    assert read_overlay.await_args.args[2] == ["疑似未注销模型三"]
    rows = {row["姓名"]: row for row in actual["inspector"]["data"]}
    assert rows["测试人员甲"]["数据总数"] == 1
    assert rows["测试人员乙"]["已完成"] == 1


@pytest.mark.asyncio
async def test_external_overlay_can_render_when_local_report_is_empty():
    with patch.object(
        stats, "get_txdocs_business_overlay", new=AsyncMock(return_value=_overlay_payload())
    ):
        actual = await stats._overlay_external_report(
            {"exists": False, "message": "本地日报尚未生成"},
            start_date="2026-09-20",
            end_date="2026-09-20",
            parser_type="全链条",
            communities=None,
        )

    assert actual["exists"] is True
    assert actual["community"]["data"][0]["数据总数"] == 2
    rows = {row["姓名"]: row for row in actual["inspector"]["data"]}
    assert set(rows) == {"测试人员甲", "测试人员乙"}
