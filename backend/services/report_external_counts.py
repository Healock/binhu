"""Shared local-report and stored read-only monitor count composition."""

from services.report_builders.summary import SUMMARY_OUTPUT_COLS, SUMMARY_INSPECTOR_OUTPUT_COLS
from services.report_range import COMMUNITY_COLUMNS, INSPECTOR_COLUMNS


def merge_external_report_counts(result: dict, overlay: dict, *, parser_type: str) -> dict:
    if not overlay.get("available") or not isinstance(result, dict):
        return result

    # A date may have no local ledger yet while an external snapshot already
    # exists.  Build empty compatible tables so the same report endpoint can
    # still show the read-only overlay instead of returning an empty report.
    if not result.get("exists"):
        community_columns = (
            SUMMARY_OUTPUT_COLS
            if parser_type == "总汇总表"
            else COMMUNITY_COLUMNS
        )
        inspector_columns = (
            SUMMARY_INSPECTOR_OUTPUT_COLS
            if parser_type == "总汇总表"
            else INSPECTOR_COLUMNS
        )
        result = {
            **result,
            "exists": True,
            "community": {"columns": list(community_columns), "data": []},
            "inspector": {"columns": list(inspector_columns), "data": []},
        }

    def external_counts(item: dict[str, int]) -> dict[str, int]:
        total = int(item.get("total", 0))
        completed = int(item.get("completed", 0))
        checked = int(item.get("checked", 0))
        unchecked = int(item.get("unchecked", 0))
        unable = int(item.get("unable_to_verify", 0))
        return {
            "数据总数": total,
            "未核查": unchecked,
            "已核查": checked,
            "已完成": completed,
            "无法见底数": unable,
        }

    def merge_counts(row: dict[str, object], item: dict[str, int], columns: list[str]) -> None:
        """Merge external counts and recalculate ratios from combined totals."""
        added = external_counts(item)
        for key, value in added.items():
            if key in columns:
                row[key] = int(row.get(key) or 0) + value
        total = int(row.get("数据总数") or 0)
        completed = int(row.get("已完成") or 0)
        unable = int(row.get("无法见底数") or 0)
        if "核查完成率" in columns:
            row["核查完成率"] = round(completed / total, 2) if total else 0
        if "核查见底率" in columns:
            row["核查见底率"] = (
                round(completed / (completed + unable), 2)
                if completed + unable else 0
            )

    community_table = result.get("community")
    if isinstance(community_table, dict) and isinstance(community_table.get("data"), list):
        rows = community_table["data"]
        by_community = {
            str(row.get("社区") or ""): row
            for row in rows
            if isinstance(row, dict)
        }
        for community, external_item in overlay.get("communities", {}).items():
            row = by_community.get(str(community))
            if row is None:
                row = {column: 0 for column in community_table.get("columns", [])}
                row["社区"] = str(community)
                rows.append(row)
            merge_counts(row, external_item, community_table.get("columns", []))

    inspector_table = result.get("inspector")
    if isinstance(inspector_table, dict) and isinstance(inspector_table.get("data"), list):
        rows = inspector_table["data"]
        columns = inspector_table.get("columns", [])
        by_assignee = {
            (str(row.get("社区") or ""), str(row.get("姓名") or "")): row
            for row in rows
            if isinstance(row, dict)
        }
        for community, external_item in overlay.get("communities", {}).items():
            assignees = external_item.get("assignees") or {
                "": external_item,
            }
            for checker_name, checker_item in assignees.items():
                display_name = str(checker_name or "").strip()
                if not display_name:
                    display_name = "未指定核查人（腾讯只读）"
                key = (str(community), display_name)
                row = by_assignee.get(key)
                if row is None:
                    row = {column: 0 for column in columns}
                    row["社区"] = str(community)
                    row["姓名"] = display_name
                    rows.append(row)
                    by_assignee[key] = row
                merge_counts(row, checker_item, columns)
    result["external_overlay"] = overlay
    return result
