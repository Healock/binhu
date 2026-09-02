"""按维护窗口把旧的未限定表名路由到新业务库。

旧代码大量使用当前连接库中的短表名。迁移期间由这个轻量 SQL 路由层把
这些固定白名单表名改成显式 schema，避免长期双写，也让每个域可以独立
切换和回退。参数仍通过数据库驱动绑定，用户输入不会进入 SQL。
"""

from __future__ import annotations

import re

import aiomysql

from config import settings


TABLE_DOMAINS: dict[str, tuple[str, str]] = {
    # table: (feature switch, target schema)
    **{table: ("PLATFORM_DOMAIN_ACTIVE", settings.MYSQL_PLATFORM_DB) for table in (
        "_users", "_sessions", "_grid_members", "_departments", "_communities", "_community_aliases",
        "_areas", "_area_leader_links", "_grid_member_department_links", "_permission_groups",
        "_position_permission_groups", "_position_permission_group_links", "_user_permission_group_links",
        "_permission_change_log", "_notifications", "_announcements", "_announcement_reads",
        "_help_documents",
        "_admin_audit_log", "_personnel_attendance_history", "_personnel_weekend_duty", "_system_config",
        "_backup_schedule", "_backup_jobs", "_work_activity_events",
        "_qmf_registration_runs", "_administrative_areas",
    )},
    **{table: ("VISIT_DOMAIN_ACTIVE", settings.MYSQL_VISIT_DB) for table in (
        "_visit_import_batches", "t_visit_details", "_visit_import_issues", "_visit_source_runs",
        "_code_summary_runs", "_code_daily_snapshots", "_code_summary_location_labels",
        "_code_summary_location_counts",
    )},
    **{table: ("DISPATCH_DOMAIN_ACTIVE", settings.MYSQL_DISPATCH_DB) for table in (
        "_police_dispatch_batches", "_police_dispatch_tasks", "_police_dispatch_publish_results",
        "_police_dispatch_publish_runs", "_police_dispatch_publish_run_items",
    )},
    **{table: ("DAILY_DOMAIN_ACTIVE", settings.MYSQL_DAILY_REPORT_DB) for table in (
        "_work_log_drafts", "_daily_task_ledger", "_daily_task_ledger_runs", "_daily_report_meta",
    )},
    **{table: ("REGISTRY_ADDRESS_DOMAIN_ACTIVE", settings.MYSQL_REGISTRY_DB) for table in (
        "_police_address_entries", "_police_address_sources", "_police_address_imports",
        "_police_address_import_conflicts", "_venue_codes", "_venue_visits", "_venue_visit_photos", "_venue_form_tokens",
    )},
}


def _enabled(switch_name: str) -> bool:
    return bool(getattr(settings, switch_name, False))


def rewrite_domain_sql(sql: str | bytes) -> str | bytes:
    """将固定白名单的旧表名改成目标 schema；已限定表名不会重复改写。"""
    is_bytes = isinstance(sql, (bytes, bytearray))
    text = sql.decode("utf-8") if is_bytes else str(sql)
    for table, (switch_name, target_schema) in sorted(TABLE_DOMAINS.items(), key=lambda item: -len(item[0])):
        if not _enabled(switch_name):
            continue
        # A few legacy queries still qualify tables with the historical
        # ``OnlineData`` schema name.  In a shadow or split-domain deployment
        # the configured online schema can be a run-scoped name (for example
        # ``LoadTest_<run>``), so recognize both spellings.  This keeps the
        # compatibility rewrite effective without creating an ``OnlineData``
        # database or granting the shadow user access to a production-named
        # schema.
        escaped_schemas = {
            re.escape(str(settings.MYSQL_ONLINE_DATA_DB)),
            re.escape("OnlineData"),
        }
        escaped_table = re.escape(table)
        # 旧代码中偶尔已经写成 OnlineData._table，也一起迁到目标库。
        qualified = re.compile(
            rf"(?:{'|'.join(sorted(escaped_schemas, key=len, reverse=True))})"
            rf"\s*\.\s*`?{escaped_table}`?",
            re.IGNORECASE,
        )
        text = qualified.sub(f"`{target_schema}`.`{table}`", text)
        # 只替换没有点号前缀的短表名。字段名、参数和已限定表名不命中。
        unqualified = re.compile(rf"(?<![A-Za-z0-9_`.])`?{escaped_table}`?(?![A-Za-z0-9_])")
        text = unqualified.sub(f"`{target_schema}`.`{table}`", text)
    return text.encode("utf-8") if is_bytes else text


class DomainRoutingCursor(aiomysql.Cursor):
    async def execute(self, query, args=None):
        return await super().execute(rewrite_domain_sql(query), args)

    async def executemany(self, query, args):
        return await super().executemany(rewrite_domain_sql(query), args)
