"""日报流水中的有效核查工作量聚合。"""

from __future__ import annotations


async def load_effective_workload_by_community(
    cur,
    start_date: str,
    end_date: str,
    parser_types: list[str],
) -> dict[str, int]:
    """按固定在线人员资格汇总区间有效工作量。"""
    if not parser_types:
        return {}
    placeholders = ",".join(["%s"] * len(parser_types))
    await cur.execute(
        f"""
        SELECT COALESCE(formal_community.name, ledger.community) AS community,
               COALESCE(SUM(ledger.effective_workload), 0)
        FROM _daily_task_ledger AS ledger
        LEFT JOIN OnlineData._community_aliases AS community_alias
          ON community_alias.alias=ledger.community
        LEFT JOIN OnlineData._communities AS formal_community
          ON formal_community.id=community_alias.community_id
        JOIN OnlineData._grid_members AS member
          ON LOWER(TRIM(member.name))=LOWER(TRIM(ledger.inspector))
        JOIN OnlineData._grid_member_department_links AS member_link
          ON member_link.member_id=member.id
        JOIN OnlineData._departments AS department
          ON department.id=member_link.department_id
         AND department.department_type='community'
        JOIN OnlineData._communities AS member_community
          ON member_community.id=department.community_id
         AND member_community.name=COALESCE(
                formal_community.name, ledger.community
             )
        WHERE ledger.report_date BETWEEN %s AND %s
          AND ledger.parser_type IN ({placeholders})
          AND ledger.included=1
          AND member.position IN ('组长', '组员')
        GROUP BY COALESCE(formal_community.name, ledger.community)
        """,
        (start_date, end_date, *parser_types),
    )
    return {
        str(community): int(workload or 0)
        for community, workload in await cur.fetchall()
    }
