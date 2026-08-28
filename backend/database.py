"""MySQL 多数据库连接池管理（同一实例内的八个业务域数据库）。"""

from contextlib import contextmanager
import re
import warnings

import aiomysql
from config import settings
from services.business_time import current_business_date
from services.permissions import (
    ALL_PERMISSIONS,
    AUTHENTICATED_PERMISSIONS,
    CODE_SUMMARY_MANAGE,
    DEFAULT_PERMISSION_GROUPS,
    ONLINE_RAW_EDIT,
    ONLINE_RAW_ROW_MANAGE,
    ONLINE_TASK_MANAGE,
    POLICE_ADDRESS_MANAGE,
    POLICE_DISPATCH_MANAGE,
    PRESENCE_DETAIL_VIEW,
    POSITION_DEFAULT_GROUP,
    QMF_REGISTRATION_EXECUTE,
    VISIT_SOURCE_MANAGE,
    WORKFLOW_ATTACHMENT_VIEW,
    WORKFLOW_TICKET_CREATE,
    WORKFLOW_TICKET_VIEW,
    parse_permissions,
    serialize_permissions,
)
from services.domain_schema import ensure_registry_schema, ensure_workflow_schema
from services.domain_routing import DomainRoutingCursor
from services.qmf_runs import ensure_qmf_registration_schema
from services.qmf_status_scan import ensure_qmf_status_scan_schema
from services.residence_status_scan import ensure_residence_status_schema
from services.task_registration import ensure_task_registration_schema
from services.unverifiable_review import ensure_unverifiable_review_schema
from services.qmf_community import seed_default_qmf_community_codes
from services.administrative_areas import ensure_administrative_area_schema
from services.parsers import TABLE_NAMES

# 数据库名称映射
DB_NAMES = {
    "online_data": settings.MYSQL_ONLINE_DATA_DB,
    "archive": settings.MYSQL_ARCHIVE_DB,
    "daily_report": settings.MYSQL_DAILY_REPORT_DB,
    "platform": settings.MYSQL_PLATFORM_DB,
    "visit": settings.MYSQL_VISIT_DB,
    "dispatch": settings.MYSQL_DISPATCH_DB,
    "registry": settings.MYSQL_REGISTRY_DB,
    "workflow": settings.MYSQL_WORKFLOW_DB,
}

OPTIONAL_DB_KEYS = {"platform", "visit", "dispatch", "registry", "workflow"}

# 所有在线业务表都可能在完整同步时产生移除记录。归档表清单直接来自
# 解析器注册中心，避免新增业务类型后只创建当前表、遗漏对应归档表。
ARCHIVE_SOURCE_TABLES = tuple(dict.fromkeys(TABLE_NAMES.values()))


@contextmanager
def suppress_expected_bootstrap_warnings():
    """只在单线程启动建表阶段忽略明确可接受的 MySQL 幂等提示。

    MySQL 会为 ``CREATE TABLE IF NOT EXISTS`` 和 ``INSERT IGNORE`` 已命中
    的记录返回 warning，aiomysql 又会逐条写到 stderr。这里不处理未知表、
    语法、截断或其他数据库 warning，避免为了日志干净而遮住真实故障。
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"^Table '.*' already exists$",
            category=Warning,
        )
        warnings.filterwarnings(
            "ignore",
            message=r"^Duplicate entry '.*' for key '.*'$",
            category=Warning,
        )
        yield


async def _ensure_column(cur, table: str, column: str, definition: str) -> None:
    await cur.execute(f"SHOW COLUMNS FROM `{table}` LIKE %s", (column,))
    if not await cur.fetchone():
        await cur.execute(
            f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}"
        )


async def _ensure_varchar_length(
    cur, table: str, column: str, minimum_length: int
) -> None:
    """Expand an existing VARCHAR without touching data or narrowing columns."""
    await cur.execute(f"SHOW COLUMNS FROM `{table}` LIKE %s", (column,))
    row = await cur.fetchone()
    if not row:
        return
    match = re.fullmatch(r"varchar\((\d+)\)", str(row[1] or "").lower())
    if match and int(match.group(1)) < minimum_length:
        await cur.execute(
            f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` VARCHAR({minimum_length})"
        )


async def _ensure_index(
    cur,
    table: str,
    index_name: str,
    definition: str,
) -> None:
    await cur.execute(f"SHOW INDEX FROM `{table}` WHERE Key_name=%s", (index_name,))
    if not await cur.fetchone():
        await cur.execute(
            f"ALTER TABLE `{table}` ADD {definition}"
        )


async def ensure_online_archive_schema(cur) -> None:
    """Ensure every parser-backed online table has a compatible archive table."""
    online_database = DB_NAMES["online_data"].replace("`", "``")
    for source_table in ARCHIVE_SOURCE_TABLES:
        archive_table = f"{source_table}_archive"
        await cur.execute(
            f"CREATE TABLE IF NOT EXISTS `{archive_table}` "
            f"LIKE `{online_database}`.`{source_table}`"
        )
        await _ensure_column(
            cur,
            archive_table,
            "_archived_at",
            "DATETIME DEFAULT CURRENT_TIMESTAMP",
        )
        await _ensure_column(
            cur,
            archive_table,
            "_archive_reason",
            "VARCHAR(100) DEFAULT 'online_removed'",
        )
        await cur.execute(
            f"SHOW INDEX FROM `{archive_table}` WHERE Key_name=%s",
            ("uk_row_key",),
        )
        if await cur.fetchone():
            await cur.execute(
                f"ALTER TABLE `{archive_table}` DROP INDEX `uk_row_key`"
            )
        await _ensure_index(
            cur,
            archive_table,
            "idx_row_key",
            "INDEX `idx_row_key` (`_row_key`)",
        )


async def ensure_permission_schema(cur) -> None:
    """增加 0.9.0 权限和部门结构，全部保持旧版本可忽略。"""
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _departments (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(200) NOT NULL UNIQUE,
            department_type VARCHAR(20) NOT NULL,
            community_id INT DEFAULT NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_department_community (community_id),
            INDEX idx_department_type_active (department_type, is_active)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute(
        "INSERT IGNORE INTO _departments "
        "(name, department_type, community_id) "
        "VALUES ('内勤', 'internal', NULL)"
    )
    await cur.execute("""
        INSERT INTO _departments (name, department_type, community_id)
        SELECT community.name, 'community', community.id
        FROM _communities AS community
        LEFT JOIN _departments AS department
          ON department.community_id=community.id
        WHERE department.id IS NULL
        ON DUPLICATE KEY UPDATE
          department_type='community',
          community_id=VALUES(community_id),
          is_active=1
    """)

    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _permission_groups (
            id INT AUTO_INCREMENT PRIMARY KEY,
            code VARCHAR(50) NOT NULL UNIQUE,
            name VARCHAR(100) NOT NULL UNIQUE,
            description VARCHAR(500) NOT NULL DEFAULT '',
            permissions JSON NOT NULL,
            data_scope VARCHAR(30) NOT NULL DEFAULT 'own_department',
            is_system TINYINT(1) NOT NULL DEFAULT 0,
            is_locked TINYINT(1) NOT NULL DEFAULT 0,
            sort_order INT NOT NULL DEFAULT 100,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    for code, group in DEFAULT_PERMISSION_GROUPS.items():
        await cur.execute(
            "INSERT IGNORE INTO _permission_groups "
            "(code, name, description, permissions, data_scope, "
            "is_system, is_locked, sort_order) "
            "VALUES (%s, %s, %s, %s, %s, 1, %s, %s)",
            (
                code,
                group["name"],
                group["description"],
                serialize_permissions(group["permissions"]),
                group["data_scope"],
                1 if code == "super_admin" else 0,
                group["sort_order"],
            ),
        )
    # 辖区档案查看是所有登录账号的基础能力。启动时只追加该只读权限，
    # 不覆盖权限组已有配置，也不扩大任何管理权限。
    await cur.execute("SELECT id, permissions FROM _permission_groups")
    for group_id, raw_permissions in await cur.fetchall():
        current = set(parse_permissions(raw_permissions))
        required = current | AUTHENTICATED_PERMISSIONS
        if required != current:
            await cur.execute(
                "UPDATE _permission_groups SET permissions=%s WHERE id=%s",
                (serialize_permissions(required), int(group_id)),
            )
    # 新权限只追加到相应预设组，不覆盖超级管理员已经调整过的其他权限。
    permission_additions = {
        "flow_post": {
            ONLINE_RAW_EDIT,
            WORKFLOW_TICKET_CREATE,
            WORKFLOW_TICKET_VIEW,
            WORKFLOW_ATTACHMENT_VIEW,
        },
        "global_viewer": {
            ONLINE_RAW_EDIT,
            ONLINE_TASK_MANAGE,
            WORKFLOW_TICKET_CREATE,
            WORKFLOW_TICKET_VIEW,
            WORKFLOW_ATTACHMENT_VIEW,
        },
        "internal_business": {
            ONLINE_RAW_EDIT,
            ONLINE_RAW_ROW_MANAGE,
            ONLINE_TASK_MANAGE,
            POLICE_ADDRESS_MANAGE,
            POLICE_DISPATCH_MANAGE,
            VISIT_SOURCE_MANAGE,
            CODE_SUMMARY_MANAGE,
            "registry.property.view",
            "registry.property.manage",
            "registry.watch.view",
            "registry.watch.manage",
            "registry.import.manage",
            WORKFLOW_TICKET_CREATE,
            WORKFLOW_TICKET_VIEW,
            "workflow.ticket.handle",
            WORKFLOW_ATTACHMENT_VIEW,
            QMF_REGISTRATION_EXECUTE,
        },
        "admin": {
            ONLINE_RAW_EDIT,
            ONLINE_RAW_ROW_MANAGE,
            ONLINE_TASK_MANAGE,
            POLICE_ADDRESS_MANAGE,
            POLICE_DISPATCH_MANAGE,
            VISIT_SOURCE_MANAGE,
            CODE_SUMMARY_MANAGE,
            "registry.property.view",
            "registry.property.manage",
            "registry.watch.view",
            "registry.watch.manage",
            "registry.import.manage",
            "workflow.ticket.handle",
            "workflow.attachment.view",
            "workflow.ticket.manage",
            QMF_REGISTRATION_EXECUTE,
            PRESENCE_DETAIL_VIEW,
        },
        "super_admin": {
            VISIT_SOURCE_MANAGE,
            CODE_SUMMARY_MANAGE,
        },
        "community_registry_viewer": {
            ONLINE_TASK_MANAGE,
            WORKFLOW_TICKET_CREATE,
            WORKFLOW_TICKET_VIEW,
            WORKFLOW_ATTACHMENT_VIEW,
        },
        "presence_detail_viewer": {
            PRESENCE_DETAIL_VIEW,
        },
    }
    for code, additions in permission_additions.items():
        await cur.execute(
            "SELECT permissions FROM _permission_groups WHERE code=%s",
            (code,),
        )
        row = await cur.fetchone()
        if not row:
            continue
        current = set(parse_permissions(row[0]))
        if not additions.issubset(current):
            await cur.execute(
                "UPDATE _permission_groups SET permissions=%s WHERE code=%s",
                (serialize_permissions(current | additions), code),
            )
    await cur.execute(
        "UPDATE _permission_groups SET permissions=%s, data_scope='all', "
        "is_system=1, is_locked=1 WHERE code='super_admin'",
        (serialize_permissions(ALL_PERMISSIONS),),
    )

    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _position_permission_groups (
            position VARCHAR(20) NOT NULL PRIMARY KEY,
            permission_group_id INT NOT NULL,
            updated_by INT DEFAULT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_position_permission_group (permission_group_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _position_permission_group_links (
            position VARCHAR(20) NOT NULL,
            permission_group_id INT NOT NULL,
            updated_by INT DEFAULT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (position, permission_group_id),
            INDEX idx_position_group_link_group (permission_group_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _user_permission_group_links (
            user_id INT NOT NULL,
            permission_group_id INT NOT NULL,
            assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, permission_group_id),
            INDEX idx_user_group_link_group (permission_group_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _permission_change_log (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            action VARCHAR(100) NOT NULL,
            target_type VARCHAR(50) NOT NULL,
            target_id VARCHAR(100) NOT NULL,
            detail JSON DEFAULT NULL,
            changed_by INT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_permission_change_target (target_type, target_id),
            INDEX idx_permission_change_time (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    for position, group_code in POSITION_DEFAULT_GROUP.items():
        await cur.execute(
            "INSERT IGNORE INTO _position_permission_groups "
            "(position, permission_group_id) "
            "SELECT %s, id FROM _permission_groups WHERE code=%s",
            (position, group_code),
        )
    await cur.execute("""
        INSERT IGNORE INTO _position_permission_group_links
            (position, permission_group_id, updated_by)
        SELECT position, permission_group_id, updated_by
        FROM _position_permission_groups
    """)
    await cur.execute("""
        INSERT IGNORE INTO _position_permission_group_links
            (position, permission_group_id)
        SELECT position_name.position, permission_group.id
        FROM (
            SELECT '片长' AS position
            UNION ALL SELECT '中队长'
            UNION ALL SELECT '社区民警'
        ) AS position_name
        JOIN _permission_groups AS permission_group
          ON permission_group.code='presence_detail_viewer'
    """)
    await cur.execute("""
        INSERT IGNORE INTO _position_permission_group_links
            (position, permission_group_id)
        SELECT position_name.position, permission_group.id
        FROM (
            SELECT '组长' AS position
            UNION ALL SELECT '组员'
        ) AS position_name
        JOIN _permission_groups AS permission_group
          ON permission_group.code='community_address_manager'
    """)
    # 0.16.0 起社区民警不再继承 admin 的全所维护权限；保留其他人工叠加组，
    # 只移除旧默认 admin 链接并换成按关联社区只读的系统组。
    await cur.execute("""
        DELETE link
        FROM _position_permission_group_links AS link
        JOIN _permission_groups AS permission_group
          ON permission_group.id=link.permission_group_id
        WHERE link.position='社区民警' AND permission_group.code='admin'
    """)
    await cur.execute("""
        INSERT IGNORE INTO _position_permission_group_links
            (position, permission_group_id)
        SELECT '社区民警', id
        FROM _permission_groups
        WHERE code='community_registry_viewer'
    """)
    await cur.execute("""
        UPDATE _position_permission_groups AS mapping
        JOIN _permission_groups AS permission_group
          ON permission_group.code='community_registry_viewer'
        SET mapping.permission_group_id=permission_group.id
        WHERE mapping.position='社区民警'
    """)

    await _ensure_column(
        cur,
        "_grid_members",
        "department_id",
        "INT DEFAULT NULL AFTER community",
    )
    await _ensure_index(
        cur,
        "_grid_members",
        "idx_grid_department",
        "INDEX idx_grid_department (department_id)",
    )
    await cur.execute("""
        UPDATE _grid_members AS member
        JOIN _departments AS department
          ON department.name='内勤' AND department.department_type='internal'
        SET member.department_id=department.id,
            member.community=''
        WHERE member.position IN ('片长', '中队长', '基础管控', '所队领导')
          AND (member.department_id IS NULL OR member.department_id<>department.id)
    """)
    await cur.execute("""
        UPDATE _grid_members AS member
        JOIN _departments AS department
          ON department.name=member.community
         AND department.department_type='community'
        SET member.department_id=department.id
        WHERE member.department_id IS NULL
          AND member.community IS NOT NULL
          AND member.community<>''
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _grid_member_department_links (
            member_id INT NOT NULL,
            department_id INT NOT NULL,
            sort_order INT NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (member_id, department_id),
            INDEX idx_member_department_department (department_id, member_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        DELETE link
        FROM _grid_member_department_links AS link
        JOIN _grid_members AS member ON member.id=link.member_id
        WHERE member.position<>'社区民警'
          AND (
              member.department_id IS NULL
              OR link.department_id<>member.department_id
          )
    """)
    await cur.execute("""
        INSERT IGNORE INTO _grid_member_department_links
            (member_id, department_id, sort_order)
        SELECT member.id, member.department_id, 0
        FROM _grid_members AS member
        WHERE member.department_id IS NOT NULL
    """)

    for column_name, definition in [
        (
            "display_name",
            "VARCHAR(100) NOT NULL DEFAULT '' AFTER username",
        ),
        ("member_id", "INT DEFAULT NULL AFTER role"),
        ("permission_group_id", "INT DEFAULT NULL AFTER member_id"),
        (
            "group_assignment_mode",
            "VARCHAR(20) NOT NULL DEFAULT 'inherited' AFTER permission_group_id",
        ),
        (
            "password_is_temporary",
            "TINYINT(1) NOT NULL DEFAULT 0 AFTER group_assignment_mode",
        ),
        (
            "active_session_id",
            "VARCHAR(64) DEFAULT NULL AFTER password_is_temporary",
        ),
        (
            "active_desktop_session_id",
            "VARCHAR(64) DEFAULT NULL AFTER active_session_id",
        ),
        (
            "active_mobile_session_id",
            "VARCHAR(64) DEFAULT NULL AFTER active_desktop_session_id",
        ),
        ("avatar_storage_key", "VARCHAR(500) DEFAULT NULL"),
        ("avatar_mime", "VARCHAR(100) DEFAULT NULL"),
    ]:
        await _ensure_column(cur, "_users", column_name, definition)
    await _ensure_index(
        cur,
        "_users",
        "uk_users_member",
        "UNIQUE INDEX uk_users_member (member_id)",
    )
    await _ensure_index(
        cur,
        "_users",
        "idx_users_permission_group",
        "INDEX idx_users_permission_group (permission_group_id)",
    )
    await _ensure_index(
        cur,
        "_users",
        "idx_users_active_session",
        "INDEX idx_users_active_session (active_session_id)",
    )
    await _ensure_index(
        cur,
        "_users",
        "idx_users_active_desktop_session",
        "INDEX idx_users_active_desktop_session (active_desktop_session_id)",
    )
    await _ensure_index(
        cur,
        "_users",
        "idx_users_active_mobile_session",
        "INDEX idx_users_active_mobile_session (active_mobile_session_id)",
    )
    await cur.execute("""
        UPDATE _users AS user
        JOIN _permission_groups AS permission_group
          ON permission_group.code=CASE
            WHEN user.role='super_admin' THEN 'super_admin'
            WHEN user.role='admin' THEN 'admin'
            ELSE NULL
          END
        SET user.permission_group_id=permission_group.id,
            user.group_assignment_mode='custom'
        WHERE user.permission_group_id IS NULL
          AND user.role IN ('super_admin', 'admin')
    """)
    await cur.execute("""
        INSERT IGNORE INTO _user_permission_group_links
            (user_id, permission_group_id)
        SELECT id, permission_group_id
        FROM _users
        WHERE group_assignment_mode='custom'
          AND permission_group_id IS NOT NULL
    """)

    await _ensure_column(
        cur,
        "_sessions",
        "last_activity_at",
        "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP AFTER created_at",
    )
    await _ensure_column(
        cur,
        "_sessions",
        "management_id",
        "CHAR(36) DEFAULT NULL AFTER session_id",
    )
    await _ensure_column(
        cur,
        "_sessions",
        "device_type",
        "VARCHAR(10) DEFAULT NULL AFTER user_id",
    )
    await _ensure_column(
        cur,
        "_sessions",
        "device_id_hash",
        "CHAR(64) DEFAULT NULL AFTER device_type",
    )
    await _ensure_column(
        cur,
        "_sessions",
        "client_platform",
        "VARCHAR(20) DEFAULT NULL AFTER device_id_hash",
    )
    await _ensure_column(
        cur,
        "_sessions",
        "user_agent_family",
        "VARCHAR(40) DEFAULT NULL AFTER client_platform",
    )
    await _ensure_index(
        cur,
        "_sessions",
        "uk_sessions_management_id",
        "UNIQUE INDEX uk_sessions_management_id (management_id)",
    )
    await _ensure_index(
        cur,
        "_sessions",
        "idx_sessions_user_device",
        "INDEX idx_sessions_user_device (user_id, device_type, expires_at)",
    )
    await _ensure_index(
        cur,
        "_sessions",
        "idx_session_user_activity",
        "INDEX idx_session_user_activity (user_id, last_activity_at)",
    )
    await cur.execute("""
        UPDATE _users AS user
        LEFT JOIN (
            SELECT session.user_id,
                   SUBSTRING_INDEX(
                       GROUP_CONCAT(
                           session.session_id
                           ORDER BY session.created_at DESC,
                                    session.session_id DESC
                       ),
                       ',', 1
                   ) AS session_id
            FROM _sessions AS session
            WHERE session.expires_at>UTC_TIMESTAMP()
            GROUP BY session.user_id
        ) AS latest ON latest.user_id=user.id
        SET user.active_session_id=latest.session_id
        WHERE user.active_session_id IS NULL
          AND latest.session_id IS NOT NULL
    """)
    # 旧版唯一会话平滑迁移为电脑端槽位；新字段已存在时重复执行无副作用。
    await cur.execute("""
        UPDATE _sessions
        SET management_id=COALESCE(management_id, UUID()),
            device_type=COALESCE(device_type, 'desktop'),
            client_platform=COALESCE(client_platform, 'web'),
            user_agent_family=COALESCE(user_agent_family, '其他浏览器')
        WHERE management_id IS NULL OR device_type IS NULL
    """)
    await cur.execute("""
        UPDATE _users
        SET active_desktop_session_id=COALESCE(
                active_desktop_session_id,
                active_session_id
            )
        WHERE active_desktop_session_id IS NULL
          AND active_session_id IS NOT NULL
    """)
    await cur.execute(
        "INSERT IGNORE INTO _system_config (config_key, config_value) "
        "VALUES ('session_idle_minutes', '30'), "
        "('permission_enforcement_enabled', '0')"
    )


async def ensure_online_editor_schema(cur) -> None:
    """增加 0.10.0 片区、腾讯来源定位和回写审计结构。"""
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _areas (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL UNIQUE,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await _ensure_column(cur, "_communities", "area_id", "INT DEFAULT NULL")
    await _ensure_column(
        cur,
        "_communities",
        "qmf_community_code",
        "VARCHAR(20) DEFAULT NULL",
    )
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _qmf_organization_codes (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            community_id INT NOT NULL,
            organization_code VARCHAR(50) NOT NULL,
            source VARCHAR(30) NOT NULL DEFAULT 'manual',
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_qmf_organization_code (organization_code),
            INDEX idx_qmf_organization_community (community_id, is_active)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await _ensure_index(
        cur,
        "_communities",
        "idx_community_area",
        "INDEX idx_community_area (area_id)",
    )
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _area_leader_links (
            area_id INT NOT NULL,
            member_id INT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (area_id, member_id),
            INDEX idx_area_leader_member (member_id, area_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)

    for area_name in ("东片", "中片", "西片"):
        await cur.execute(
            "INSERT IGNORE INTO _areas (name) VALUES (%s)",
            (area_name,),
        )
    initial_communities = {
        "东片": ("长板", "龙河", "祥泰", "江城"),
        "中片": ("冬梅", "三船港", "联团", "湖滨华城"),
        "西片": ("顾家荡", "水秀", "阅湖", "南厍"),
    }
    for area_name, community_names in initial_communities.items():
        placeholders = ", ".join(["%s"] * len(community_names))
        await cur.execute(
            f"UPDATE _communities AS community "
            f"JOIN _areas AS area ON area.name=%s "
            f"SET community.area_id=COALESCE(community.area_id, area.id) "
            f"WHERE community.name IN ({placeholders})",
            (area_name, *community_names),
        )
    await seed_default_qmf_community_codes(cur)
    for area_name, leader_name in (
        ("东片", "熊朝良"),
        ("中片", "褚寿生"),
        ("西片", "蔡泉波"),
    ):
        await cur.execute(
            """
            INSERT IGNORE INTO _area_leader_links (area_id, member_id)
            SELECT area.id, member.id
            FROM _areas AS area
            JOIN _grid_members AS member
              ON member.name=%s AND member.position='片长'
            WHERE area.name=%s
            """,
            (leader_name, area_name),
        )

    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _online_source_rows (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            spreadsheet_id INT NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            sheet_id VARCHAR(100) NOT NULL,
            physical_row INT NOT NULL,
            row_key CHAR(32) NOT NULL,
            row_hash CHAR(64) NOT NULL,
            values_json JSON NOT NULL,
            cell_meta_json JSON NOT NULL,
            revision BIGINT UNSIGNED NOT NULL DEFAULT 1,
            refreshed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_online_source_position (
                spreadsheet_id, sheet_id, physical_row
            ),
            INDEX idx_online_source_business (
                parser_type, row_key, spreadsheet_id
            )
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _online_source_projection (
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            values_json JSON NOT NULL,
            community VARCHAR(200) NOT NULL DEFAULT '',
            inspector VARCHAR(100) NOT NULL DEFAULT '',
            identity_hmac CHAR(64) DEFAULT NULL,
            first_dispatch_at DATETIME DEFAULT NULL,
            task_state VARCHAR(20) NOT NULL DEFAULT '',
            source_count INT NOT NULL DEFAULT 1,
            conflict TINYINT(1) NOT NULL DEFAULT 0,
            search_text MEDIUMTEXT NOT NULL,
            pending_state VARCHAR(20) NOT NULL DEFAULT '',
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (parser_type, row_key),
            INDEX idx_source_projection_community (parser_type, community),
            INDEX idx_source_projection_pending (parser_type, pending_state),
            INDEX idx_source_projection_tasks (
                parser_type, community, inspector, task_state
            ),
            INDEX idx_source_projection_identity (
                parser_type, identity_hmac
            )
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await _ensure_column(
        cur,
        "_online_source_projection",
        "inspector",
        "VARCHAR(100) NOT NULL DEFAULT '' AFTER community",
    )
    await _ensure_column(
        cur,
        "_online_source_projection",
        "task_state",
        "VARCHAR(20) NOT NULL DEFAULT '' AFTER inspector",
    )
    await _ensure_column(
        cur,
        "_online_source_projection",
        "identity_hmac",
        "CHAR(64) DEFAULT NULL AFTER inspector",
    )
    await _ensure_column(
        cur,
        "_online_source_projection",
        "first_dispatch_at",
        "DATETIME DEFAULT NULL AFTER identity_hmac",
    )
    await _ensure_index(
        cur,
        "_online_source_projection",
        "idx_source_projection_tasks",
        "INDEX idx_source_projection_tasks "
        "(parser_type, community, inspector, task_state)",
    )
    await _ensure_index(
        cur,
        "_online_source_projection",
        "idx_source_projection_identity",
        "INDEX idx_source_projection_identity (parser_type, identity_hmac)",
    )
    # 只读取已有来源缓存完成兼容回填，不访问或改写腾讯文档。
    await cur.execute("""
        UPDATE _online_source_projection
        SET inspector=TRIM(COALESCE(
                JSON_UNQUOTE(JSON_EXTRACT(values_json, '$.\"核查人\"')),
                ''
            )),
            task_state=CASE
                WHEN parser_type='疑似未注销模型三' THEN
                    CASE WHEN TRIM(COALESCE(
                        JSON_UNQUOTE(JSON_EXTRACT(values_json, '$.\"核查结果\"')),
                        ''
                    )) IN ('近期返吴', '近期反吴', '在吴', '离吴', '非本辖区')
                    THEN 'completed' ELSE 'unchecked' END
                WHEN parser_type='疑似返苏' THEN
                    CASE
                        WHEN TRIM(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(
                            values_json, '$.\"核查反馈\"'
                        )), ''))<>'' THEN 'completed'
                        WHEN TRIM(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(
                            values_json, '$.\"现住址\"'
                        )), ''))<>'' THEN 'checked'
                        ELSE 'unchecked'
                    END
                WHEN parser_type IN (
                    '全链条', '出租房屋核查', '寄递业'
                ) THEN
                    CASE
                        WHEN TRIM(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(
                            values_json, '$.\"核查结果\"'
                        )), ''))<>'' THEN 'completed'
                        WHEN TRIM(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(
                            values_json, '$.\"现住址\"'
                        )), ''))<>'' THEN 'checked'
                        ELSE 'unchecked'
                    END
                ELSE ''
            END
        WHERE task_state=''
          AND parser_type IN (
              '全链条', '出租房屋核查', '寄递业',
              '疑似未注销模型三', '疑似返苏'
          )
    """)
    # 修复旧版本把正确结果“近期返吴”或“非本辖区”误判为未核查的投影状态。
    # 历史错拼值仍按已完成兼容；这里只更新本地投影，不写腾讯来源表。
    await cur.execute("""
        UPDATE _online_source_projection
        SET task_state='completed'
        WHERE parser_type='疑似未注销模型三'
          AND task_state<>'completed'
          AND TRIM(COALESCE(
              JSON_UNQUOTE(JSON_EXTRACT(values_json, '$.\"核查结果\"')),
              ''
          )) IN ('近期返吴', '近期反吴', '非本辖区')
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _online_source_cache_state (
            spreadsheet_id INT NOT NULL PRIMARY KEY,
            parser_type VARCHAR(50) NOT NULL,
            row_count INT NOT NULL DEFAULT 0,
            refreshed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_source_cache_parser (parser_type, refreshed_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _online_writeback_audit (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            username VARCHAR(50) NOT NULL DEFAULT '',
            action VARCHAR(20) NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            spreadsheet_id INT NOT NULL,
            sheet_id VARCHAR(100) NOT NULL,
            physical_row INT DEFAULT NULL,
            column_name VARCHAR(200) DEFAULT NULL,
            row_key_before CHAR(32) DEFAULT NULL,
            row_key_after CHAR(32) DEFAULT NULL,
            before_values JSON DEFAULT NULL,
            after_values JSON DEFAULT NULL,
            sync_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            synced_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_writeback_audit_time (created_at),
            INDEX idx_writeback_audit_pending (
                spreadsheet_id, sync_status, created_at
            ),
            INDEX idx_writeback_audit_user (user_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _online_local_changes (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            audit_id BIGINT NOT NULL,
            source_id BIGINT NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            spreadsheet_id INT NOT NULL,
            sheet_id VARCHAR(100) NOT NULL,
            physical_row INT NOT NULL,
            row_key CHAR(32) NOT NULL,
            field_name VARCHAR(200) NOT NULL,
            base_value TEXT NOT NULL,
            local_value TEXT NOT NULL,
            remote_value TEXT DEFAULT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
            next_attempt_at DATETIME DEFAULT NULL,
            error_code VARCHAR(100) NOT NULL DEFAULT '',
            last_error VARCHAR(500) NOT NULL DEFAULT '',
            user_id INT NOT NULL,
            username VARCHAR(50) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_online_local_change_field (source_id, field_name),
            INDEX idx_online_local_change_audit (audit_id, status),
            INDEX idx_online_local_change_due (
                status, next_attempt_at, updated_at
            ),
            INDEX idx_online_local_change_row (
                parser_type, row_key, status
            ),
            INDEX idx_online_local_change_source (source_id, status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute(
        "INSERT IGNORE INTO _system_config (config_key, config_value) "
        "VALUES ('online_writeback_enabled', '0')"
    )
    await cur.execute(
        "DELETE FROM _online_writeback_audit "
        "WHERE created_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 90 DAY)"
    )
    await cur.execute(
        "UPDATE _online_writeback_audit SET sync_status='failed' "
        "WHERE sync_status='writing'"
    )
    await cur.execute(
        "UPDATE _online_local_changes "
        "SET status='retry', next_attempt_at=UTC_TIMESTAMP(), "
        "error_code='service_restarted', "
        "last_error='服务重启后等待重新同步' "
        "WHERE status='processing'"
    )
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _external_acquisition_runs (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            kind VARCHAR(50) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'queued',
            phase VARCHAR(50) NOT NULL DEFAULT 'queued',
            current_count INT DEFAULT 0,
            total_count INT DEFAULT NULL,
            progress_message VARCHAR(500) NOT NULL DEFAULT '',
            requested_by BIGINT DEFAULT NULL,
            payload_json JSON DEFAULT NULL,
            result_json JSON DEFAULT NULL,
            dedupe_key VARCHAR(190) DEFAULT NULL,
            error_code VARCHAR(60) DEFAULT NULL,
            error_message VARCHAR(500) DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME DEFAULT NULL,
            finished_at DATETIME DEFAULT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_external_runs_kind_created (kind, created_at),
            INDEX idx_external_runs_status (status, created_at),
            INDEX idx_external_runs_dedupe (kind, dedupe_key, status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)


async def ensure_police_dispatch_schema(cur) -> None:
    """增加公安地址库、预处理和发布对账的兼容结构。"""
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _police_address_entries (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(300) NOT NULL,
            normalized_name VARCHAR(300) NOT NULL,
            detail_address VARCHAR(1000) NOT NULL DEFAULT '',
            address_type VARCHAR(30) NOT NULL DEFAULT 'community',
            pattern VARCHAR(200) NOT NULL DEFAULT '',
            community_id INT DEFAULT NULL,
            aliases_json JSON NOT NULL,
            source_flags JSON NOT NULL,
            enabled TINYINT(1) NOT NULL DEFAULT 1,
            created_by INT DEFAULT NULL,
            updated_by INT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_police_address_name_community (
                normalized_name, community_id
            ),
            INDEX idx_police_address_community (community_id, enabled),
            INDEX idx_police_address_type (address_type, enabled)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _police_address_imports (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            import_kind VARCHAR(30) NOT NULL,
            file_name VARCHAR(255) NOT NULL DEFAULT '',
            file_sha256 CHAR(64) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'preview',
            imported_count INT NOT NULL DEFAULT 0,
            created_count INT NOT NULL DEFAULT 0,
            merged_count INT NOT NULL DEFAULT 0,
            conflict_count INT NOT NULL DEFAULT 0,
            created_by INT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_police_address_import_hash (
                import_kind, file_sha256
            ),
            INDEX idx_police_address_import_time (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _police_address_sources (
            entry_id BIGINT NOT NULL,
            import_id BIGINT NOT NULL,
            source_kind VARCHAR(30) NOT NULL,
            source_row INT NOT NULL,
            source_name VARCHAR(300) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (entry_id, import_id, source_row),
            INDEX idx_police_address_source_import (import_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _police_address_import_conflicts (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            import_id BIGINT DEFAULT NULL,
            source_row INT NOT NULL DEFAULT 0,
            reason VARCHAR(500) NOT NULL,
            values_json JSON NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_police_address_conflict_import (import_id, id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _police_dispatch_batches (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            file_name VARCHAR(255) NOT NULL DEFAULT '',
            file_sha256 CHAR(64) NOT NULL,
            sheet_name VARCHAR(255) NOT NULL DEFAULT '',
            import_mode VARCHAR(20) NOT NULL DEFAULT 'raw',
            status VARCHAR(30) NOT NULL DEFAULT 'reviewing',
            total_count INT NOT NULL DEFAULT 0,
            counts_json JSON NOT NULL,
            imported_by INT NOT NULL,
            first_publish_date DATE DEFAULT NULL,
            publish_started_at DATETIME DEFAULT NULL,
            completed_at DATETIME DEFAULT NULL,
            last_error VARCHAR(500) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_police_dispatch_batch_status (status, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _police_dispatch_tasks (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            batch_id BIGINT NOT NULL,
            source_row INT NOT NULL,
            source_name VARCHAR(300) NOT NULL DEFAULT '',
            person_name VARCHAR(200) NOT NULL DEFAULT '',
            identity_number VARCHAR(50) NOT NULL DEFAULT '',
            identity_hash CHAR(64) NOT NULL DEFAULT '',
            phone VARCHAR(200) NOT NULL DEFAULT '',
            original_address VARCHAR(1500) NOT NULL DEFAULT '',
            source_created_time VARCHAR(100) NOT NULL DEFAULT '',
            transfer_note TEXT,
            raw_values_json JSON NOT NULL,
            duplicate_group_key CHAR(64) NOT NULL DEFAULT '',
            duplicate_kind VARCHAR(30) NOT NULL DEFAULT '',
            suggested_action VARCHAR(30) NOT NULL DEFAULT 'dispatch',
            suggested_community_id INT DEFAULT NULL,
            suggestion_reason VARCHAR(1000) NOT NULL DEFAULT '',
            allocation_mode VARCHAR(30) NOT NULL DEFAULT '',
            final_action VARCHAR(30) NOT NULL DEFAULT '',
            final_community_id INT DEFAULT NULL,
            review_note VARCHAR(1000) NOT NULL DEFAULT '',
            reviewed_by INT DEFAULT NULL,
            reviewer_name VARCHAR(100) NOT NULL DEFAULT '',
            reviewed_at DATETIME DEFAULT NULL,
            version INT UNSIGNED NOT NULL DEFAULT 1,
            task_status VARCHAR(30) NOT NULL DEFAULT 'pending_review',
            publish_status VARCHAR(30) NOT NULL DEFAULT 'not_required',
            published_row INT DEFAULT NULL,
            publish_key CHAR(64) NOT NULL DEFAULT '',
            publish_error VARCHAR(500) NOT NULL DEFAULT '',
            linked_source_id BIGINT DEFAULT NULL,
            linked_row_hash CHAR(64) NOT NULL DEFAULT '',
            conflict_values_json JSON DEFAULT NULL,
            cache_pending TINYINT(1) NOT NULL DEFAULT 0,
            published_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_police_dispatch_source_row (batch_id, source_row),
            INDEX idx_police_dispatch_task_filter (
                batch_id, task_status, suggested_action
            ),
            INDEX idx_police_dispatch_task_duplicate (
                batch_id, duplicate_group_key
            ),
            INDEX idx_police_dispatch_task_identity (batch_id, identity_hash),
            INDEX idx_police_dispatch_task_publish (batch_id, publish_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _police_dispatch_publish_results (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            task_id BIGINT NOT NULL UNIQUE,
            spreadsheet_id INT NOT NULL,
            sheet_id VARCHAR(100) NOT NULL,
            physical_row INT DEFAULT NULL,
            business_key CHAR(64) NOT NULL DEFAULT '',
            request_values_json JSON NOT NULL,
            verified_values_json JSON DEFAULT NULL,
            source_row_id BIGINT DEFAULT NULL,
            expected_row_hash CHAR(64) NOT NULL DEFAULT '',
            resolution VARCHAR(30) NOT NULL DEFAULT '',
            cache_pending TINYINT(1) NOT NULL DEFAULT 0,
            status VARCHAR(30) NOT NULL DEFAULT 'pending',
            error_code VARCHAR(100) NOT NULL DEFAULT '',
            error_message VARCHAR(500) NOT NULL DEFAULT '',
            attempt_count INT NOT NULL DEFAULT 0,
            last_attempt_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_police_publish_status (status, updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _police_dispatch_publish_runs (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            batch_id BIGINT NOT NULL,
            spreadsheet_id INT NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'pending',
            phase VARCHAR(30) NOT NULL DEFAULT 'queued',
            total_count INT NOT NULL DEFAULT 0,
            processed_count INT NOT NULL DEFAULT 0,
            success_count INT NOT NULL DEFAULT 0,
            conflict_count INT NOT NULL DEFAULT 0,
            reconciliation_count INT NOT NULL DEFAULT 0,
            retryable_count INT NOT NULL DEFAULT 0,
            requested_by INT DEFAULT NULL,
            requested_username VARCHAR(50) NOT NULL DEFAULT '',
            error_code VARCHAR(100) NOT NULL DEFAULT '',
            error_message VARCHAR(500) NOT NULL DEFAULT '',
            started_at DATETIME DEFAULT NULL,
            finished_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_police_publish_run_batch (batch_id, id),
            INDEX idx_police_publish_run_active (spreadsheet_id, status, id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _police_dispatch_publish_run_items (
            run_id BIGINT NOT NULL,
            task_id BIGINT NOT NULL,
            item_order INT NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'queued',
            physical_row INT DEFAULT NULL,
            error_code VARCHAR(100) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, task_id),
            INDEX idx_police_publish_run_item_status (run_id, status, item_order),
            INDEX idx_police_publish_run_item_task (task_id, run_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _police_dispatch_import_issues (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            batch_id BIGINT NOT NULL,
            task_id BIGINT DEFAULT NULL,
            source_row INT NOT NULL,
            field_name VARCHAR(100) NOT NULL DEFAULT '',
            issue_type VARCHAR(50) NOT NULL,
            safe_value VARCHAR(200) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_police_import_issue_batch (batch_id, source_row),
            INDEX idx_police_import_issue_task (task_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS t_suzhou_police (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            _row_key VARCHAR(200) NOT NULL,
            `下发日期` VARCHAR(500), `截止日期` VARCHAR(500),
            `核查人` VARCHAR(500), `社区` VARCHAR(500), `姓名` VARCHAR(500),
            `身份证号` VARCHAR(500), `联系号码` VARCHAR(500),
            `疑似现住址` VARCHAR(500), `接警编号` VARCHAR(500),
            `出警日期` VARCHAR(500), `出警类别` VARCHAR(500),
            `出警内容` VARCHAR(500), `出警单位` VARCHAR(500),
            `参考派出所` VARCHAR(500), `现住址` VARCHAR(500),
            `核查结果` VARCHAR(500), `研判` VARCHAR(500),
            `二次反馈` VARCHAR(500), `备注` VARCHAR(500),
            _first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            _last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_suzhou_police_row_key (_row_key),
            INDEX idx_suzhou_police_community (`社区`),
            INDEX idx_suzhou_police_inspector (`核查人`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS t_traffic_police (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            _row_key VARCHAR(200) NOT NULL,
            `下发日期` VARCHAR(500), `截止日期` VARCHAR(500),
            `核查人` VARCHAR(500), `社区` VARCHAR(500), `姓名` VARCHAR(500),
            `身份证号` VARCHAR(500), `联系号码` VARCHAR(500),
            `地址1` VARCHAR(500), `现住址` VARCHAR(500),
            `核查结果` VARCHAR(500), `研判` VARCHAR(500),
            `二次反馈` VARCHAR(500), `备注` VARCHAR(500),
            _first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            _last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_traffic_police_row_key (_row_key),
            INDEX idx_traffic_police_community (`社区`),
            INDEX idx_traffic_police_inspector (`核查人`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _fullchain_police_raw_uploads (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            file_name VARCHAR(255) NOT NULL DEFAULT '',
            file_sha256 CHAR(64) NOT NULL,
            sheet_name VARCHAR(255) NOT NULL DEFAULT '',
            row_count INT NOT NULL DEFAULT 0,
            invalid_count INT NOT NULL DEFAULT 0,
            duplicate_count INT NOT NULL DEFAULT 0,
            storage_key VARCHAR(500) NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'confirmed',
            uploaded_by INT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_fullchain_raw_upload_sha (file_sha256),
            INDEX idx_fullchain_raw_upload_time (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _fullchain_police_raw_identities (
            upload_id BIGINT NOT NULL,
            identity_hmac CHAR(64) NOT NULL,
            PRIMARY KEY (upload_id, identity_hmac),
            INDEX idx_fullchain_raw_identity (identity_hmac, upload_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await _ensure_column(cur, "_fullchain_police_raw_uploads", "storage_key", "VARCHAR(500) NOT NULL DEFAULT ''")
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _fullchain_archive_reviews (
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            decision VARCHAR(30) NOT NULL,
            note VARCHAR(500) NOT NULL DEFAULT '',
            decided_by INT DEFAULT NULL,
            decided_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (parser_type, row_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _fullchain_archive_exports (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            export_no VARCHAR(40) NOT NULL UNIQUE,
            parser_type VARCHAR(50) NOT NULL DEFAULT '全链条',
            status VARCHAR(30) NOT NULL DEFAULT 'queued',
            phase VARCHAR(30) NOT NULL DEFAULT 'queued',
            file_name VARCHAR(255) NOT NULL DEFAULT '',
            storage_key VARCHAR(500) NOT NULL DEFAULT '',
            file_sha256 CHAR(64) NOT NULL DEFAULT '',
            total_count INT NOT NULL DEFAULT 0,
            success_count INT NOT NULL DEFAULT 0,
            conflict_count INT NOT NULL DEFAULT 0,
            error_count INT NOT NULL DEFAULT 0,
            categories_json JSON NOT NULL,
            requested_by INT DEFAULT NULL,
            error_message VARCHAR(500) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME DEFAULT NULL,
            finished_at DATETIME DEFAULT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_fullchain_archive_export_time (created_at),
            INDEX idx_fullchain_archive_export_status (status, created_at),
            INDEX idx_fullchain_archive_export_parser (parser_type, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await _ensure_column(
        cur, "_fullchain_archive_exports", "parser_type",
        "VARCHAR(50) NOT NULL DEFAULT '全链条' AFTER export_no",
    )
    await _ensure_index(
        cur,
        "_fullchain_archive_exports",
        "idx_fullchain_archive_export_parser",
        "INDEX `idx_fullchain_archive_export_parser` (`parser_type`, `created_at`)",
    )
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _fullchain_archive_export_items (
            export_id BIGINT NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            source_id BIGINT NOT NULL,
            spreadsheet_id INT NOT NULL,
            sheet_id VARCHAR(100) NOT NULL,
            physical_row INT NOT NULL,
            expected_revision BIGINT NOT NULL,
            expected_row_hash CHAR(64) NOT NULL,
            source_values_json JSON DEFAULT NULL,
            category VARCHAR(40) NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'queued',
            error_code VARCHAR(100) NOT NULL DEFAULT '',
            external_delete_state VARCHAR(30) NOT NULL DEFAULT 'pending',
            external_deleted_at DATETIME DEFAULT NULL,
            PRIMARY KEY (export_id, source_id),
            INDEX idx_fullchain_archive_item_status (export_id, status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await _ensure_column(
        cur, "_fullchain_archive_export_items", "source_values_json",
        "JSON DEFAULT NULL AFTER expected_row_hash",
    )
    await _ensure_column(
        cur, "_fullchain_archive_export_items", "external_delete_state",
        "VARCHAR(30) NOT NULL DEFAULT 'pending' AFTER error_code",
    )
    await _ensure_column(
        cur, "_fullchain_archive_export_items", "external_deleted_at",
        "DATETIME DEFAULT NULL AFTER external_delete_state",
    )

    for table_name, columns in {
        "_police_address_imports": {
            "created_count": "INT NOT NULL DEFAULT 0",
        },
        "_police_dispatch_tasks": {
            "linked_source_id": "BIGINT DEFAULT NULL",
            "linked_row_hash": "CHAR(64) NOT NULL DEFAULT ''",
            "conflict_values_json": "JSON DEFAULT NULL",
            "cache_pending": "TINYINT(1) NOT NULL DEFAULT 0",
            "standard_values_json": "JSON DEFAULT NULL",
            "business_key_hmac": "CHAR(64) NOT NULL DEFAULT ''",
            "validation_issues_json": "JSON DEFAULT NULL",
        },
        "_police_dispatch_batches": {
            "import_mode": "VARCHAR(20) NOT NULL DEFAULT 'raw'",
            "business_type": "VARCHAR(30) NOT NULL DEFAULT 'fullchain'",
            "police_subtype": "VARCHAR(30) NOT NULL DEFAULT ''",
            "import_profile": "VARCHAR(50) NOT NULL DEFAULT 'fullchain_raw'",
            "adapter_version": "VARCHAR(30) NOT NULL DEFAULT ''",
            "target_parser": "VARCHAR(50) NOT NULL DEFAULT '全链条'",
            "business_date": "DATE DEFAULT NULL",
            "source_summary_json": "JSON DEFAULT NULL",
            "storage_key": "VARCHAR(500) NOT NULL DEFAULT ''",
        },
        "_police_dispatch_publish_results": {
            "source_row_id": "BIGINT DEFAULT NULL",
            "expected_row_hash": "CHAR(64) NOT NULL DEFAULT ''",
            "resolution": "VARCHAR(30) NOT NULL DEFAULT ''",
            "cache_pending": "TINYINT(1) NOT NULL DEFAULT 0",
        },
    }.items():
        for column_name, column_definition in columns.items():
            await cur.execute(
                f"SHOW COLUMNS FROM `{table_name}` LIKE %s",
                (column_name,),
            )
            if not await cur.fetchone():
                await cur.execute(
                    f"ALTER TABLE `{table_name}` "
                    f"ADD COLUMN `{column_name}` {column_definition}"
                )

    await cur.execute("""
        UPDATE _police_dispatch_batches
        SET business_type='fullchain',
            import_profile=CASE import_mode
                WHEN 'clean' THEN 'fullchain_processed'
                ELSE 'fullchain_raw' END,
            target_parser='全链条'
        WHERE business_type='' OR import_profile='' OR target_parser=''
    """)

    # 0.26.0：同一份文件可能属于不同业务入口。移除旧的单列唯一索引，
    # 改为“适配器 + 文件摘要”组合唯一；索引迁移幂等且不触碰批次数据。
    await cur.execute("""
        SELECT index_name
        FROM information_schema.statistics
        WHERE table_schema=DATABASE()
          AND table_name='_police_dispatch_batches'
          AND non_unique=0
          AND index_name<>'PRIMARY'
        GROUP BY index_name
        HAVING COUNT(*)=1
           AND MAX(column_name)='file_sha256'
    """)
    for (index_name,) in await cur.fetchall():
        safe_index = str(index_name).replace("`", "``")
        await cur.execute(
            f"ALTER TABLE `_police_dispatch_batches` DROP INDEX `{safe_index}`"
        )
    await _ensure_index(
        cur,
        "_police_dispatch_batches",
        "uk_police_dispatch_profile_file",
        "UNIQUE INDEX uk_police_dispatch_profile_file "
        "(import_profile, file_sha256)",
    )

    # 0.13.1：手机号为空的记录不能继续自动下发。只重新打开尚未产生
    # 腾讯外部结果的任务；已成功、发布中、待对账或冲突任务保持原状。
    missing_phone_reason = "缺少手机号，需基础管控先研判；补齐手机号后才能下发"
    await cur.execute("""
        UPDATE _police_dispatch_tasks AS task
        JOIN _police_dispatch_batches AS batch ON batch.id=task.batch_id
        SET task.suggested_action='manual', task.suggested_community_id=NULL,
            task.suggestion_reason=%s, task.allocation_mode='missing_phone',
            task.final_action='', task.final_community_id=NULL, task.review_note='',
            task.reviewed_by=NULL, task.reviewer_name='', task.reviewed_at=NULL,
            task.task_status='pending_review', task.publish_status='not_required',
            task.publish_error='', task.version=task.version+1
        WHERE batch.target_parser='全链条'
          AND TRIM(task.phone)=''
          AND (
              (task.task_status='pending_review' AND task.final_action='')
              OR (
                  task.final_action='dispatch'
                  AND task.publish_status IN ('pending', 'retryable', 'not_required')
              )
          )
          AND NOT (
              task.suggested_action='manual'
              AND task.suggested_community_id IS NULL
              AND task.allocation_mode='missing_phone'
              AND task.suggestion_reason=%s
              AND task.final_action=''
          )
    """, (missing_phone_reason, missing_phone_reason))
    await cur.execute("""
        UPDATE _police_dispatch_batches AS batch
        SET status='reviewing', completed_at=NULL
        WHERE batch.status<>'reviewing'
          AND EXISTS (
              SELECT 1 FROM _police_dispatch_tasks AS task
              WHERE task.batch_id=batch.id
                AND task.task_status='pending_review'
          )
    """)


async def ensure_work_activity_schema(cur) -> None:
    """Add permanent, privacy-safe work contribution events for 0.13.0."""
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _work_activity_events (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            event_key VARCHAR(190) NOT NULL,
            profile_key VARCHAR(64) NOT NULL,
            user_id INT NOT NULL,
            member_id INT DEFAULT NULL,
            activity_type VARCHAR(50) NOT NULL,
            units INT UNSIGNED NOT NULL DEFAULT 1,
            occurred_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_work_activity_event (event_key),
            INDEX idx_work_activity_profile_time (profile_key, occurred_at),
            INDEX idx_work_activity_user_time (user_id, occurred_at),
            INDEX idx_work_activity_member_time (member_id, occurred_at),
            INDEX idx_work_activity_type_time (activity_type, occurred_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)

    # Copy no business values into this table: only actor, type, count and time.
    await cur.execute("""
        INSERT IGNORE INTO _work_activity_events (
            event_key, profile_key, user_id, member_id,
            activity_type, units, occurred_at, created_at
        )
        SELECT CONCAT('writeback:', audit.id),
               CASE WHEN user.member_id IS NOT NULL
                    THEN CONCAT('member:', user.member_id)
                    ELSE CONCAT('user:', audit.user_id) END,
               audit.user_id, user.member_id,
               'online_task_update', 1, audit.created_at, UTC_TIMESTAMP()
        FROM _online_writeback_audit AS audit
        JOIN _users AS user ON user.id=audit.user_id
        WHERE audit.action='update'
          AND audit.sync_status IN ('pending', 'synced')
          AND COALESCE(audit.column_name, '') REGEXP
              '(^|、)(现住址|核查结果|核查反馈|实际情况|二次反馈|二次核查结果|二次反馈/二次核查结果|研判|登记情况)(、|$)'
    """)

    await cur.execute("""
        INSERT IGNORE INTO _work_activity_events (
            event_key, profile_key, user_id, member_id,
            activity_type, units, occurred_at, created_at
        )
        SELECT CONCAT('admin-audit:', audit.id),
               CASE WHEN user.member_id IS NOT NULL
                    THEN CONCAT('member:', user.member_id)
                    ELSE CONCAT('user:', audit.user_id) END,
               audit.user_id, user.member_id,
               CASE
                   WHEN audit.action IN (
                       'police_dispatch.review',
                       'police_dispatch.bulk_review'
                   ) THEN 'police_dispatch_review'
                   ELSE 'work_log'
               END,
               CASE
                   WHEN audit.action='police_dispatch.bulk_review'
                   THEN GREATEST(
                       1,
                       COALESCE(CAST(JSON_UNQUOTE(
                           JSON_EXTRACT(audit.detail_json, '$.count')
                       ) AS UNSIGNED), 1)
                   )
                   ELSE 1
               END,
               audit.created_at, UTC_TIMESTAMP()
        FROM _admin_audit_log AS audit
        JOIN _users AS user ON user.id=audit.user_id
        WHERE audit.result='success'
          AND audit.action IN (
              'police_dispatch.review',
              'police_dispatch.bulk_review',
              'work_log.create'
          )
    """)

    # Old create audits may have expired. A surviving draft still proves one
    # creation, but does not justify inventing historical save activity.
    await cur.execute("""
        INSERT IGNORE INTO _work_activity_events (
            event_key, profile_key, user_id, member_id,
            activity_type, units, occurred_at, created_at
        )
        SELECT CONCAT('work-log-draft:', draft.id),
               CASE WHEN user.member_id IS NOT NULL
                    THEN CONCAT('member:', user.member_id)
                    ELSE CONCAT('user:', draft.created_by) END,
               draft.created_by, user.member_id,
               'work_log', 1, draft.created_at, UTC_TIMESTAMP()
        FROM _work_log_drafts AS draft
        JOIN _users AS user ON user.id=draft.created_by
        WHERE NOT EXISTS (
            SELECT 1
            FROM _admin_audit_log AS audit
            WHERE audit.action='work_log.create'
              AND audit.result='success'
              AND audit.target_type='work_log_draft'
              AND CAST(audit.target_name AS BINARY)=CAST(draft.id AS BINARY)
        )
    """)


async def ensure_bootstrap_admin(cur) -> bool:
    """Create the first super administrator only from explicit environment values."""
    await cur.execute("SELECT COUNT(*) FROM _users")
    if (await cur.fetchone())[0] > 0:
        return False

    username = settings.BOOTSTRAP_ADMIN_USERNAME.strip()
    password = settings.BOOTSTRAP_ADMIN_PASSWORD
    if not username or not password:
        raise RuntimeError(
            "用户表为空。请临时设置 BOOTSTRAP_ADMIN_USERNAME 和 "
            "BOOTSTRAP_ADMIN_PASSWORD 创建首个超级管理员。"
        )

    import bcrypt

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    await cur.execute(
        "INSERT INTO _users "
        "(username, password_hash, role, permission_group_id, "
        "group_assignment_mode) "
        "SELECT %s, %s, 'super_admin', id, 'custom' "
        "FROM _permission_groups WHERE code='super_admin'",
        (username, password_hash),
    )
    print(f"[DB] 初始超级管理员已创建: {username}")
    return True


class DatabaseManager:
    """管理同一 MySQL 实例中的八个业务域连接池。"""
    _pools: dict[str, aiomysql.Pool] = {}

    @classmethod
    async def init_all(cls):
        """创建八个数据库的连接池；迁移期的新域连接可选。"""
        for key, db_name in DB_NAMES.items():
            if key in OPTIONAL_DB_KEYS and not settings.MYSQL_DOMAIN_DATABASES_ENABLED:
                continue
            try:
                cls._pools[key] = await aiomysql.create_pool(
                    host=settings.MYSQL_HOST,
                    port=settings.MYSQL_PORT,
                    user=settings.MYSQL_USER,
                    password=settings.MYSQL_PASSWORD,
                    db=db_name,
                    minsize=2,
                    maxsize=settings.MYSQL_POOL_SIZE,
                    charset="utf8mb4",
                    autocommit=True,
                    cursorclass=DomainRoutingCursor,
                )
            except Exception as exc:
                if key in OPTIONAL_DB_KEYS and "unknown database" in str(exc).lower():
                    print(f"[DB] optional domain database is not ready: {db_name}")
                    continue
                raise
        # 确保新表存在（不需要删 volume 重建）
        async with cls._pools["online_data"].acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _grid_members (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        name VARCHAR(100) NOT NULL,
                        community VARCHAR(200) DEFAULT '',
                        position VARCHAR(20) NOT NULL DEFAULT '组员',
                        phone VARCHAR(50) DEFAULT '',
                        notes VARCHAR(500) DEFAULT '',
                        status VARCHAR(10) NOT NULL DEFAULT '在岗',
                        leave_start_date DATE DEFAULT NULL,
                        leave_end_date DATE DEFAULT NULL,
                        leave_reason VARCHAR(200) DEFAULT '',
                        leave_source VARCHAR(30) NOT NULL DEFAULT 'manual',
                        id_card_number VARCHAR(50) DEFAULT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_name (name),
                        UNIQUE KEY uk_grid_id_card (id_card_number)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _personnel_attendance_history (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        member_id INT NOT NULL,
                        absence_type VARCHAR(30) NOT NULL,
                        start_date DATE NOT NULL,
                        end_date DATE DEFAULT NULL,
                        reason VARCHAR(200) DEFAULT '',
                        source VARCHAR(30) NOT NULL DEFAULT 'manual',
                        created_by INT DEFAULT NULL,
                        is_active TINYINT(1) NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_attendance_member_dates (
                            member_id, start_date, end_date
                        ),
                        INDEX idx_attendance_active (
                            is_active, start_date, end_date
                        )
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _personnel_weekend_duty (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        week_start DATE NOT NULL,
                        member_id INT NOT NULL,
                        duty_date DATE DEFAULT NULL,
                        member_name VARCHAR(100) NOT NULL,
                        community_snapshot VARCHAR(200) DEFAULT '',
                        position_snapshot VARCHAR(20) NOT NULL,
                        updated_by INT DEFAULT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_weekend_member (
                            week_start, member_id
                        ),
                        INDEX idx_weekend_duty_date (duty_date),
                        INDEX idx_weekend_week (week_start)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _communities (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        name VARCHAR(200) NOT NULL UNIQUE,
                        police_officers JSON DEFAULT NULL,
                        is_active TINYINT(1) NOT NULL DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute(
                    "SHOW COLUMNS FROM _communities LIKE 'police_officers'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "ALTER TABLE _communities "
                        "ADD COLUMN police_officers JSON DEFAULT NULL AFTER name"
                    )
                await cur.execute(
                    "SHOW COLUMNS FROM _communities LIKE 'is_active'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "ALTER TABLE _communities "
                        "ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1"
                    )
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _community_aliases (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        community_id INT NOT NULL,
                        alias VARCHAR(200) NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_community_alias (alias),
                        INDEX idx_community_alias_owner (community_id),
                        CONSTRAINT fk_community_alias_owner
                            FOREIGN KEY (community_id)
                            REFERENCES _communities(id)
                            ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _system_config (
                        config_key VARCHAR(100) PRIMARY KEY,
                        config_value TEXT
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute(
                    "INSERT IGNORE INTO _system_config (config_key, config_value) VALUES ('timezone', 'Asia/Shanghai')"
                )
                await cur.execute(
                    "INSERT IGNORE INTO _system_config "
                    "(config_key, config_value) VALUES "
                    "('online_summary_positions', '[\"组长\", \"组员\"]'), "
                    "('visit_summary_positions', '[\"组长\", \"组员\"]'), "
                    "('weekend_duty_positions', '[\"组长\", \"组员\"]')"
                )
                await cur.execute(
                    "INSERT IGNORE INTO _system_config "
                    "(config_key, config_value) VALUES "
                    "('maintenance_enabled', '0'), "
                    "('maintenance_start_at', ''), "
                    "('maintenance_end_at', ''), "
                    "('maintenance_message', '平台正在维护中，请稍后再试')"
                )
                await cur.execute(
                    "SELECT config_value FROM _system_config "
                    "WHERE config_key='timezone'"
                )
                timezone_row = await cur.fetchone()
                history_started_on = current_business_date(
                    timezone_row[0] if timezone_row else None
                )
                await cur.execute(
                    "INSERT IGNORE INTO _system_config "
                    "(config_key, config_value) VALUES "
                    "('attendance_history_started_on', %s)",
                    (history_started_on.isoformat(),),
                )
                await cur.execute(
                    "SELECT config_value FROM _system_config "
                    "WHERE config_key='attendance_history_started_on'"
                )
                history_started_row = await cur.fetchone()
                history_started_text = (
                    str(history_started_row[0])
                    if history_started_row and history_started_row[0]
                    else history_started_on.isoformat()
                )
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _sync_schedule (
                        id TINYINT NOT NULL PRIMARY KEY,
                        enabled TINYINT(1) NOT NULL DEFAULT 1,
                        interval_minutes INT NOT NULL DEFAULT 10,
                        next_run_at DATETIME DEFAULT NULL,
                        last_triggered_at DATETIME DEFAULT NULL,
                        updated_by INT DEFAULT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute(
                    "INSERT IGNORE INTO _sync_schedule "
                    "(id, enabled, interval_minutes, next_run_at) "
                    "VALUES (1, 1, 10, DATE_ADD(UTC_TIMESTAMP(), INTERVAL 10 MINUTE))"
                )
                await cur.execute(
                    """
                    UPDATE _sync_schedule
                    SET interval_minutes=10,
                        next_run_at=CASE
                            WHEN enabled=1 THEN DATE_ADD(
                                UTC_TIMESTAMP(), INTERVAL 10 MINUTE
                            )
                            ELSE NULL
                        END
                    WHERE id=1
                      AND NOT EXISTS (
                          SELECT 1
                          FROM _system_config
                          WHERE config_key='sync_interval_10m_migrated'
                      )
                    """
                )
                await cur.execute(
                    "INSERT IGNORE INTO _system_config "
                    "(config_key, config_value) "
                    "VALUES ('sync_interval_10m_migrated', '1')"
                )
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _notifications (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NOT NULL,
                        category VARCHAR(30) NOT NULL DEFAULT 'sync',
                        severity VARCHAR(20) NOT NULL DEFAULT 'error',
                        title VARCHAR(100) NOT NULL,
                        content TEXT NOT NULL,
                        related_task_id INT DEFAULT NULL,
                        is_read TINYINT(1) NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        read_at DATETIME DEFAULT NULL,
                        UNIQUE KEY uk_notification_user_task (
                            user_id, category, related_task_id
                        ),
                        INDEX idx_notification_unread (
                            user_id, is_read, created_at
                        )
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _announcements (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        system_key VARCHAR(100) DEFAULT NULL,
                        severity VARCHAR(20) NOT NULL DEFAULT 'info',
                        title VARCHAR(100) NOT NULL,
                        content TEXT NOT NULL,
                        is_active TINYINT(1) NOT NULL DEFAULT 1,
                        published_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        expires_at DATETIME DEFAULT NULL,
                        created_by INT DEFAULT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_announcement_system_key (system_key),
                        INDEX idx_announcement_active_time (
                            is_active, published_at, expires_at
                        )
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _announcement_reads (
                        announcement_id BIGINT NOT NULL,
                        user_id INT NOT NULL,
                        read_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (announcement_id, user_id),
                        INDEX idx_announcement_read_user (
                            user_id, read_at
                        )
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute(
                    """
                    INSERT IGNORE INTO _announcements (
                        system_key, severity, title, content,
                        is_active, published_at
                    ) VALUES (%s, 'warning', %s, %s, 1, UTC_TIMESTAMP())
                    """,
                    (
                        "attendance-history-started-on",
                        "部分日期早于系统开始保存出勤历史的时间",
                        (
                            f"出勤历史从 {history_started_text} 开始完整保存；"
                            "更早的请假记录如果没有补录，人均值只能作为参考。"
                        ),
                    ),
                )
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _backup_schedule (
                        id TINYINT NOT NULL PRIMARY KEY,
                        enabled TINYINT(1) NOT NULL DEFAULT 1,
                        run_hour TINYINT NOT NULL DEFAULT 2,
                        run_minute TINYINT NOT NULL DEFAULT 0,
                        retention_days INT NOT NULL DEFAULT 7,
                        next_run_at DATETIME DEFAULT NULL,
                        last_triggered_at DATETIME DEFAULT NULL,
                        updated_by INT DEFAULT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute(
                    "INSERT IGNORE INTO _backup_schedule "
                    "(id, enabled, run_hour, run_minute, retention_days) "
                    "VALUES (1, 1, 2, 0, 7)"
                )
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _backup_jobs (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        trigger_source VARCHAR(20) NOT NULL DEFAULT 'manual',
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        requested_by INT DEFAULT NULL,
                        filename VARCHAR(255) DEFAULT NULL,
                        size_bytes BIGINT DEFAULT NULL,
                        sha256 CHAR(64) DEFAULT NULL,
                        error_message TEXT DEFAULT NULL,
                        started_at DATETIME DEFAULT NULL,
                        finished_at DATETIME DEFAULT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_backup_status (status),
                        INDEX idx_backup_created (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _txdocs_api_usage_hourly (
                        bucket_hour DATETIME NOT NULL,
                        request_source VARCHAR(40) NOT NULL DEFAULT 'unknown',
                        endpoint VARCHAR(40) NOT NULL,
                        method VARCHAR(10) NOT NULL,
                        attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
                        success_count INT UNSIGNED NOT NULL DEFAULT 0,
                        failure_count INT UNSIGNED NOT NULL DEFAULT 0,
                        retry_count INT UNSIGNED NOT NULL DEFAULT 0,
                        quota_exhausted_count INT UNSIGNED NOT NULL DEFAULT 0,
                        last_http_status SMALLINT DEFAULT NULL,
                        last_error_code VARCHAR(40) NOT NULL DEFAULT '',
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (
                            bucket_hour, request_source, endpoint, method
                        ),
                        INDEX idx_txdocs_usage_hour (bucket_hour)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _admin_audit_log (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT DEFAULT NULL,
                        username VARCHAR(50) NOT NULL DEFAULT '',
                        action VARCHAR(80) NOT NULL,
                        target_type VARCHAR(50) NOT NULL DEFAULT '',
                        target_name VARCHAR(200) NOT NULL DEFAULT '',
                        result VARCHAR(20) NOT NULL DEFAULT 'success',
                        detail_json JSON DEFAULT NULL,
                        ip_address VARCHAR(45) NOT NULL DEFAULT '',
                        user_agent VARCHAR(300) NOT NULL DEFAULT '',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_audit_user_time (user_id, created_at),
                        INDEX idx_audit_action_time (action, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _work_log_drafts (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        report_type VARCHAR(20) NOT NULL DEFAULT 'daily',
                        business_date DATE NOT NULL,
                        owner_user_id INT NOT NULL,
                        owner_username VARCHAR(50) NOT NULL DEFAULT '',
                        template_version VARCHAR(30) NOT NULL DEFAULT 'daily-v2',
                        system_snapshot JSON NOT NULL,
                        manual_values JSON NOT NULL,
                        override_values JSON NOT NULL,
                        version INT UNSIGNED NOT NULL DEFAULT 1,
                        last_export_at DATETIME DEFAULT NULL,
                        created_by INT NOT NULL,
                        updated_by INT NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_work_log_type_date (
                            report_type, business_date
                        ),
                        INDEX idx_work_log_owner_date (
                            owner_user_id, business_date
                        )
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _visit_import_batches (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        import_type VARCHAR(20) NOT NULL DEFAULT 'detail',
                        filename VARCHAR(255) NOT NULL,
                        file_sha256 CHAR(64) NOT NULL,
                        file_size_bytes BIGINT NOT NULL DEFAULT 0,
                        status VARCHAR(20) NOT NULL DEFAULT 'running',
                        uploader_id INT DEFAULT NULL,
                        source_type VARCHAR(20) NOT NULL DEFAULT 'manual',
                        source_run_id BIGINT DEFAULT NULL,
                        sheet_name VARCHAR(100) DEFAULT NULL,
                        total_rows INT NOT NULL DEFAULT 0,
                        valid_rows INT NOT NULL DEFAULT 0,
                        inserted_rows INT NOT NULL DEFAULT 0,
                        updated_rows INT NOT NULL DEFAULT 0,
                        unchanged_rows INT NOT NULL DEFAULT 0,
                        ignored_rows INT NOT NULL DEFAULT 0,
                        error_count INT NOT NULL DEFAULT 0,
                        warning_count INT NOT NULL DEFAULT 0,
                        file_start_date DATE DEFAULT NULL,
                        file_end_date DATE DEFAULT NULL,
                        overlap_start_date DATE DEFAULT NULL,
                        overlap_end_date DATE DEFAULT NULL,
                        error_message TEXT DEFAULT NULL,
                        started_at DATETIME DEFAULT NULL,
                        finished_at DATETIME DEFAULT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_visit_batch_hash (file_sha256),
                        INDEX idx_visit_batch_type_hash (
                            import_type, file_sha256, status
                        ),
                        INDEX idx_visit_batch_status (status),
                        INDEX idx_visit_batch_source (source_type, source_run_id),
                        INDEX idx_visit_batch_created (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS t_visit_details (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        _row_key CHAR(64) NOT NULL,
                        派出所名称 VARCHAR(200) DEFAULT '',
                        村社区 VARCHAR(200) NOT NULL,
                        社区 VARCHAR(200) NOT NULL,
                        进入方式 VARCHAR(20) NOT NULL,
                        地址 VARCHAR(1000) NOT NULL,
                        _normalized_address VARCHAR(1000) NOT NULL,
                        _address_key CHAR(64) NOT NULL,
                        操作人 VARCHAR(100) NOT NULL,
                        操作人账号 VARCHAR(50) DEFAULT '',
                        入户时间 DATETIME NOT NULL,
                        业务日期 DATE NOT NULL,
                        _raw_visit_time VARCHAR(100) DEFAULT '',
                        房间核查数量 INT UNSIGNED NOT NULL DEFAULT 0,
                        新增 INT UNSIGNED NOT NULL DEFAULT 0,
                        变更 INT UNSIGNED NOT NULL DEFAULT 0,
                        注销 INT UNSIGNED NOT NULL DEFAULT 0,
                        星级派出所名称 VARCHAR(200) DEFAULT NULL,
                        星级所属社区 VARCHAR(200) DEFAULT NULL,
                        星级社区 VARCHAR(200) DEFAULT NULL,
                        星级地址 VARCHAR(1000) DEFAULT NULL,
                        得分 DECIMAL(18, 6) DEFAULT NULL,
                        星级 VARCHAR(50) DEFAULT NULL,
                        星级采集时间 DATETIME DEFAULT NULL,
                        星级采集日期 DATE DEFAULT NULL,
                        _raw_star_time VARCHAR(100) DEFAULT NULL,
                        隐患详情 MEDIUMTEXT DEFAULT NULL,
                        星级时间差秒 INT UNSIGNED DEFAULT NULL,
                        star_import_batch_id BIGINT DEFAULT NULL,
                        star_source_row_number INT DEFAULT NULL,
                        import_batch_id BIGINT NOT NULL,
                        source_row_number INT NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_visit_row_key (_row_key),
                        INDEX idx_visit_date (业务日期),
                        INDEX idx_visit_community_date (社区, 业务日期),
                        INDEX idx_visit_operator_date (操作人, 业务日期),
                        INDEX idx_visit_address_date (_address_key, 业务日期),
                        INDEX idx_visit_batch (import_batch_id),
                        INDEX idx_visit_star_time (星级采集时间),
                        INDEX idx_visit_star_batch (star_import_batch_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute(
                    "SHOW COLUMNS FROM _visit_import_batches "
                    "LIKE 'import_type'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "ALTER TABLE _visit_import_batches "
                        "ADD COLUMN import_type VARCHAR(20) NOT NULL "
                        "DEFAULT 'detail' AFTER id"
                    )
                await cur.execute(
                    "SHOW INDEX FROM _visit_import_batches "
                    "WHERE Key_name='idx_visit_batch_type_hash'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "CREATE INDEX idx_visit_batch_type_hash "
                        "ON _visit_import_batches "
                        "(import_type, file_sha256, status)"
                    )
                for column_name, column_definition in [
                    ("source_type", "VARCHAR(20) NOT NULL DEFAULT 'manual'"),
                    ("source_run_id", "BIGINT DEFAULT NULL"),
                ]:
                    await _ensure_column(
                        cur,
                        "_visit_import_batches",
                        column_name,
                        column_definition,
                    )
                await _ensure_index(
                    cur,
                    "_visit_import_batches",
                    "idx_visit_batch_source",
                    "INDEX idx_visit_batch_source (source_type, source_run_id)",
                )
                visit_star_columns = [
                    ("星级派出所名称", "VARCHAR(200) DEFAULT NULL"),
                    ("星级所属社区", "VARCHAR(200) DEFAULT NULL"),
                    ("星级社区", "VARCHAR(200) DEFAULT NULL"),
                    ("星级地址", "VARCHAR(1000) DEFAULT NULL"),
                    ("得分", "DECIMAL(18, 6) DEFAULT NULL"),
                    ("星级", "VARCHAR(50) DEFAULT NULL"),
                    ("星级采集时间", "DATETIME DEFAULT NULL"),
                    ("星级采集日期", "DATE DEFAULT NULL"),
                    ("_raw_star_time", "VARCHAR(100) DEFAULT NULL"),
                    ("隐患详情", "MEDIUMTEXT DEFAULT NULL"),
                    ("星级时间差秒", "INT UNSIGNED DEFAULT NULL"),
                    ("star_import_batch_id", "BIGINT DEFAULT NULL"),
                    ("star_source_row_number", "INT DEFAULT NULL"),
                ]
                for column_name, column_definition in visit_star_columns:
                    await cur.execute(
                        "SHOW COLUMNS FROM t_visit_details LIKE %s",
                        (column_name,),
                    )
                    if not await cur.fetchone():
                        await cur.execute(
                            f"ALTER TABLE t_visit_details "
                            f"ADD COLUMN `{column_name}` {column_definition}"
                        )
                for index_name, index_sql in [
                    (
                        "idx_visit_star_time",
                        "CREATE INDEX idx_visit_star_time "
                        "ON t_visit_details (`星级采集时间`)",
                    ),
                    (
                        "idx_visit_star_batch",
                        "CREATE INDEX idx_visit_star_batch "
                        "ON t_visit_details (star_import_batch_id)",
                    ),
                ]:
                    await cur.execute(
                        "SHOW INDEX FROM t_visit_details "
                        "WHERE Key_name=%s",
                        (index_name,),
                    )
                    if not await cur.fetchone():
                        await cur.execute(index_sql)
                # 兼容已经导入的数据：正式匹配值去掉“社区”或“村”后缀，
                # 原始“村社区”字段保持不变。
                await cur.execute("""
                    UPDATE t_visit_details
                    SET 社区 = CASE
                        WHEN RIGHT(TRIM(村社区), 2) = '社区'
                            THEN TRIM(LEFT(
                                TRIM(村社区),
                                CHAR_LENGTH(TRIM(村社区)) - 2
                            ))
                        WHEN RIGHT(TRIM(村社区), 1) = '村'
                            THEN TRIM(LEFT(
                                TRIM(村社区),
                                CHAR_LENGTH(TRIM(村社区)) - 1
                            ))
                        ELSE TRIM(村社区)
                    END
                    WHERE 村社区 IS NOT NULL
                """)
                # 去重主键包含网格员姓名：同日同地址由不同网格员走访时，
                # 每名网格员各保留一条。
                await cur.execute("""
                    UPDATE t_visit_details
                    SET _row_key = LOWER(SHA2(CONCAT(
                        DATE_FORMAT(业务日期, '%Y-%m-%d'),
                        '|',
                        _normalized_address,
                        '|',
                        TRIM(操作人)
                    ), 256))
                """)
                await cur.execute("""
                    UPDATE t_visit_details AS v
                    JOIN _community_aliases AS a
                      ON a.alias = v.社区
                    JOIN _communities AS c
                      ON c.id = a.community_id
                    SET v.社区 = c.name
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _visit_import_issues (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        batch_id BIGINT NOT NULL,
                        severity VARCHAR(20) NOT NULL,
                        code VARCHAR(60) NOT NULL,
                        source_row_number INT NOT NULL DEFAULT 0,
                        message VARCHAR(500) NOT NULL,
                        row_preview JSON DEFAULT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_visit_issue_batch (
                            batch_id, severity, source_row_number
                        )
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                    """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _visit_source_runs (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        source_kind VARCHAR(20) NOT NULL,
                        trigger_source VARCHAR(20) NOT NULL DEFAULT 'manual',
                        status VARCHAR(30) NOT NULL DEFAULT 'preview',
                        requested_by INT DEFAULT NULL,
                        requested_start_date DATE NOT NULL,
                        requested_end_date DATE NOT NULL,
                        response_business_date DATE DEFAULT NULL,
                        source_page VARCHAR(120) NOT NULL,
                        source_url VARCHAR(500) DEFAULT NULL,
                        record_count INT NOT NULL DEFAULT 0,
                        valid_count INT NOT NULL DEFAULT 0,
                        issue_count INT NOT NULL DEFAULT 0,
                        summary_json JSON DEFAULT NULL,
                        payload_json JSON DEFAULT NULL,
                        error_code VARCHAR(60) DEFAULT NULL,
                        error_message VARCHAR(500) DEFAULT NULL,
                        confirmed_by INT DEFAULT NULL,
                        confirmed_at DATETIME DEFAULT NULL,
                        superseded_by BIGINT DEFAULT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_visit_source_kind_status (source_kind, status),
                        INDEX idx_visit_source_dates (requested_start_date, requested_end_date),
                        INDEX idx_visit_source_created (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                    """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _code_summary_runs (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        source_kind VARCHAR(20) NOT NULL,
                        trigger_source VARCHAR(20) NOT NULL DEFAULT 'manual',
                        status VARCHAR(20) NOT NULL,
                        requested_by INT DEFAULT NULL,
                        requested_start_date DATE NOT NULL,
                        requested_end_date DATE NOT NULL,
                        source_endpoint VARCHAR(190) NOT NULL,
                        raw_count INT NOT NULL DEFAULT 0,
                        valid_count INT NOT NULL DEFAULT 0,
                        excluded_count INT NOT NULL DEFAULT 0,
                        duplicate_count INT NOT NULL DEFAULT 0,
                        unclassified_count INT NOT NULL DEFAULT 0,
                        source_hash CHAR(64) NOT NULL DEFAULT '',
                        classifier_version VARCHAR(20) NOT NULL DEFAULT 'v1',
                        summary_json JSON DEFAULT NULL,
                        error_code VARCHAR(60) DEFAULT NULL,
                        error_message VARCHAR(500) DEFAULT NULL,
                        finished_at DATETIME DEFAULT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_code_run_source_created (source_kind, created_at),
                        INDEX idx_code_run_dates (requested_start_date, requested_end_date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _code_daily_snapshots (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        source_kind VARCHAR(20) NOT NULL,
                        business_date DATE NOT NULL,
                        version_no INT UNSIGNED NOT NULL,
                        run_id BIGINT NOT NULL,
                        raw_count INT NOT NULL DEFAULT 0,
                        total_people INT NOT NULL DEFAULT 0,
                        patrol_scan_count INT NOT NULL DEFAULT 0,
                        dispatch_hall_scan_count INT NOT NULL DEFAULT 0,
                        household_hall_scan_count INT NOT NULL DEFAULT 0,
                        social_scan_count INT NOT NULL DEFAULT 0,
                        unclassified_scan_count INT NOT NULL DEFAULT 0,
                        active_accounts INT NOT NULL DEFAULT 0,
                        instruction_count INT NOT NULL DEFAULT 0,
                        new_registration_count INT NOT NULL DEFAULT 0,
                        excluded_identity_count INT NOT NULL DEFAULT 0,
                        duplicate_removed_count INT NOT NULL DEFAULT 0,
                        classifier_version VARCHAR(20) NOT NULL DEFAULT 'v1',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_code_snapshot_version (
                            source_kind, business_date, version_no
                        ),
                        INDEX idx_code_snapshot_latest (
                            source_kind, business_date, version_no
                        ),
                        INDEX idx_code_snapshot_run (run_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                    """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _code_summary_location_labels (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        source_kind VARCHAR(20) NOT NULL,
                        location_key VARCHAR(255) NOT NULL,
                        display_name VARCHAR(255) NOT NULL DEFAULT '',
                        classification VARCHAR(30) NOT NULL,
                        enabled TINYINT(1) NOT NULL DEFAULT 1,
                        created_by INT DEFAULT NULL,
                        updated_by INT DEFAULT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_code_location_label (source_kind, location_key),
                        INDEX idx_code_location_label_class (source_kind, classification, enabled)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _code_summary_location_counts (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        run_id BIGINT NOT NULL,
                        source_kind VARCHAR(20) NOT NULL,
                        business_date DATE NOT NULL,
                        location_key VARCHAR(255) NOT NULL,
                        display_name VARCHAR(255) NOT NULL DEFAULT '',
                        classification VARCHAR(30) NOT NULL DEFAULT 'unclassified',
                        row_count INT NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_code_location_count (run_id, business_date, location_key),
                        INDEX idx_code_location_count_range (source_kind, business_date, location_key),
                        INDEX idx_code_location_count_run (run_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                for column_name, column_definition in [
                    (
                        "trigger_source",
                        "VARCHAR(20) NOT NULL DEFAULT 'manual'",
                    ),
                    ("requested_by", "INT DEFAULT NULL"),
                    ("phase", "VARCHAR(30) NOT NULL DEFAULT 'queued'"),
                    ("current_item", "VARCHAR(200) DEFAULT NULL"),
                    ("total_steps", "INT NOT NULL DEFAULT 0"),
                    ("completed_steps", "INT NOT NULL DEFAULT 0"),
                ]:
                    await cur.execute(
                        "SHOW COLUMNS FROM _sync_log LIKE %s",
                        (column_name,),
                    )
                    if not await cur.fetchone():
                        await cur.execute(
                            f"ALTER TABLE _sync_log "
                            f"ADD COLUMN `{column_name}` {column_definition}"
                        )
                await cur.execute(
                    "UPDATE _sync_log SET phase='finished' "
                    "WHERE status IN ('success', 'completed', 'partial', 'failed') "
                    "AND phase='queued'"
                )
                # 疑似返苏表列名修正（身份证号→身份证号码，二次反馈→二次核查结果）
                for old_name, new_name, col_type in [
                    ("身份证号", "身份证号码", "VARCHAR(50)"),
                    ("二次反馈", "二次核查结果", "VARCHAR(500)"),
                ]:
                    try:
                        await cur.execute(
                            f"ALTER TABLE t_suspect_return CHANGE COLUMN `{old_name}` `{new_name}` {col_type}"
                        )
                    except Exception:
                        pass  # 列名已改过或表不存在
                # 电话号码列扩展为 VARCHAR(500)（多选号码拼接后可能超过50字符）
                for table, col in [
                    ("t_fullchain", "电话号码"),
                    ("t_rental_check", "手机号码"),
                    ("t_delivery_industry", "手机号码"),
                    ("t_suspect_unrevoked", "联系方式"),
                    ("t_suspect_return", "联系号码"),
                    ("t_suspect_return", "身份证号码"),
                ]:
                    try:
                        await cur.execute(
                            f"ALTER TABLE {table} MODIFY COLUMN `{col}` VARCHAR(500)"
                        )
                    except Exception:
                        pass
                # 全链条腾讯来源表新增的“登记情况”是正式业务字段。可空列保持
                # 旧程序可回退，历史数据等待下一次正常同步补齐。
                await _ensure_column(
                    cur,
                    "t_fullchain",
                    "登记情况",
                    "VARCHAR(500) DEFAULT NULL AFTER `地址`",
                )
                # 涉警来源的备注是网格员可补充的业务字段；旧库需要平滑补列。
                await _ensure_column(
                    cur,
                    "t_suzhou_police",
                    "备注",
                    "VARCHAR(500) DEFAULT NULL AFTER `二次反馈`",
                )
                await _ensure_column(
                    cur,
                    "t_traffic_police",
                    "备注",
                    "VARCHAR(500) DEFAULT NULL AFTER `二次反馈`",
                )
                # 旧数据库平滑补齐网格员状态和请假字段
                for column_name, column_definition in [
                    ("position", "VARCHAR(20) NOT NULL DEFAULT '组员'"),
                    ("status", "VARCHAR(10) NOT NULL DEFAULT '在岗'"),
                    ("leave_start_date", "DATE DEFAULT NULL"),
                    ("leave_end_date", "DATE DEFAULT NULL"),
                    ("leave_reason", "VARCHAR(200) DEFAULT ''"),
                    ("leave_source", "VARCHAR(30) NOT NULL DEFAULT 'manual'"),
                    ("id_card_number", "VARCHAR(50) DEFAULT NULL"),
                ]:
                    await cur.execute(
                        "SHOW COLUMNS FROM _grid_members LIKE %s", (column_name,)
                    )
                    if not await cur.fetchone():
                        await cur.execute(
                            f"ALTER TABLE _grid_members "
                            f"ADD COLUMN `{column_name}` {column_definition}"
                        )
                await cur.execute(
                    "SHOW INDEX FROM _grid_members "
                    "WHERE Key_name='uk_grid_id_card'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "ALTER TABLE _grid_members "
                        "ADD UNIQUE KEY uk_grid_id_card (id_card_number)"
                    )
                await cur.execute(
                    "SHOW INDEX FROM _grid_members "
                    "WHERE Key_name='idx_grid_position'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "ALTER TABLE _grid_members "
                        "ADD INDEX idx_grid_position (position)"
                    )
                await cur.execute("""
                    INSERT INTO _personnel_attendance_history (
                        member_id, absence_type, start_date, end_date,
                        reason, source
                    )
                    SELECT
                        g.id,
                        CASE
                            WHEN g.status = '离岗'
                            THEN 'long_term_leave'
                            ELSE 'temporary_leave'
                        END,
                        CASE
                            WHEN g.status = '离岗'
                            THEN COALESCE(DATE(g.updated_at), CURRENT_DATE)
                            ELSE g.leave_start_date
                        END,
                        CASE
                            WHEN g.status = '离岗'
                            THEN NULL
                            ELSE g.leave_end_date
                        END,
                        COALESCE(g.leave_reason, ''),
                        'legacy_current_state'
                    FROM _grid_members AS g
                    WHERE (
                        g.status = '离岗'
                        OR (
                            g.leave_start_date IS NOT NULL
                            AND g.leave_end_date IS NOT NULL
                        )
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM _personnel_attendance_history AS history
                        WHERE history.member_id = g.id
                          AND history.is_active=1
                          AND (
                              (
                                  g.status = '离岗'
                                  AND history.absence_type='long_term_leave'
                                  AND history.end_date IS NULL
                              )
                              OR (
                                  g.status <> '离岗'
                                  AND history.absence_type='temporary_leave'
                                  AND history.start_date=g.leave_start_date
                                  AND history.end_date=g.leave_end_date
                              )
                          )
                    )
                """)
                # 用户表
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _users (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        username VARCHAR(50) NOT NULL UNIQUE,
                        display_name VARCHAR(100) NOT NULL DEFAULT '',
                        password_hash VARCHAR(255) NOT NULL,
                        role ENUM('super_admin','admin','leader','member') NOT NULL DEFAULT 'member',
                        table_display_mode VARCHAR(10) NOT NULL DEFAULT 'table',
                        task_display_mode VARCHAR(10) NOT NULL DEFAULT 'table',
                        report_column_mode VARCHAR(10) NOT NULL DEFAULT 'three',
                        mobile_navigation_mode VARCHAR(10) NOT NULL DEFAULT 'dock',
                        mobile_dock_config JSON DEFAULT NULL,
                        theme_mode VARCHAR(10) NOT NULL DEFAULT 'light',
                        active_session_id VARCHAR(64) DEFAULT NULL,
                        active_desktop_session_id VARCHAR(64) DEFAULT NULL,
                        active_mobile_session_id VARCHAR(64) DEFAULT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                # 旧用户表平滑补齐账号级个性化设置
                for column_name, column_definition in [
                    (
                        "display_name",
                        "VARCHAR(100) NOT NULL DEFAULT '' AFTER username",
                    ),
                    (
                        "table_display_mode",
                        "VARCHAR(10) NOT NULL DEFAULT 'table'",
                    ),
                    (
                        "task_display_mode",
                        "VARCHAR(10) NOT NULL DEFAULT 'table'",
                    ),
                    (
                        "report_column_mode",
                        "VARCHAR(10) NOT NULL DEFAULT 'three'",
                    ),
                    (
                        "mobile_navigation_mode",
                        "VARCHAR(10) NOT NULL DEFAULT 'dock'",
                    ),
                    (
                        "mobile_dock_config",
                        "JSON DEFAULT NULL",
                    ),
                    (
                        "theme_mode",
                        "VARCHAR(10) NOT NULL DEFAULT 'light'",
                    ),
                    (
                        "avatar_storage_key",
                        "VARCHAR(500) DEFAULT NULL",
                    ),
                    (
                        "avatar_mime",
                        "VARCHAR(100) DEFAULT NULL",
                    ),
                    (
                        "active_session_id",
                        "VARCHAR(64) DEFAULT NULL",
                    ),
                    (
                        "active_desktop_session_id",
                        "VARCHAR(64) DEFAULT NULL",
                    ),
                    (
                        "active_mobile_session_id",
                        "VARCHAR(64) DEFAULT NULL",
                    ),
                ]:
                    await cur.execute(
                        "SHOW COLUMNS FROM _users LIKE %s",
                        (column_name,),
                    )
                    if not await cur.fetchone():
                        await cur.execute(
                            f"ALTER TABLE _users "
                            f"ADD COLUMN `{column_name}` {column_definition}"
                        )
                await cur.execute(
                    "SELECT config_value FROM _system_config "
                    "WHERE config_key='desktop_task_table_default_migrated'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "ALTER TABLE _users MODIFY COLUMN task_display_mode "
                        "VARCHAR(10) NOT NULL DEFAULT 'table'"
                    )
                    await cur.execute(
                        "UPDATE _users SET task_display_mode='table' "
                        "WHERE task_display_mode='card'"
                    )
                    await cur.execute(
                        "INSERT INTO _system_config (config_key, config_value) "
                        "VALUES ('desktop_task_table_default_migrated', '1')"
                    )
                # Session 表
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _sessions (
                        session_id VARCHAR(64) PRIMARY KEY,
                        management_id CHAR(36) UNIQUE,
                        user_id INT NOT NULL,
                        device_type VARCHAR(10) DEFAULT NULL,
                        device_id_hash CHAR(64) DEFAULT NULL,
                        client_platform VARCHAR(20) DEFAULT NULL,
                        user_agent_family VARCHAR(40) DEFAULT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        last_activity_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        expires_at DATETIME NOT NULL,
                        INDEX idx_user (user_id),
                        INDEX idx_expires (expires_at),
                        INDEX idx_sessions_user_device (user_id, device_type, expires_at),
                        INDEX idx_session_user_activity (user_id, last_activity_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _user_presence_clients (
                        client_id VARCHAR(64) PRIMARY KEY,
                        user_id INT NOT NULL,
                        session_id VARCHAR(64) NOT NULL,
                        last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_presence_last_seen (last_seen_at),
                        INDEX idx_presence_user (user_id),
                        INDEX idx_presence_session (session_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await ensure_permission_schema(cur)
                await ensure_online_editor_schema(cur)
                await ensure_police_dispatch_schema(cur)
                await ensure_work_activity_schema(cur)
                await ensure_qmf_registration_schema(cur)
                await ensure_qmf_status_scan_schema(cur)
                await ensure_residence_status_schema(cur)
                await ensure_task_registration_schema(cur)
                await ensure_unverifiable_review_schema(cur)
                await ensure_administrative_area_schema(cur)
                await ensure_bootstrap_admin(cur)

        # 归档查询和后续移除归档使用与当前表相同的标准字段；旧归档表也要
        # 在启动时平滑补齐，既不改历史记录，也不要求重建归档库。
        async with cls._pools["archive"].acquire() as conn:
            async with conn.cursor() as cur:
                await ensure_online_archive_schema(cur)
                await _ensure_column(
                    cur,
                    "t_fullchain_archive",
                    "登记情况",
                    "VARCHAR(500) DEFAULT NULL AFTER `地址`",
                )
                await _ensure_varchar_length(
                    cur, "t_fullchain_archive", "电话号码", 500
                )

        async with cls._pools["daily_report"].acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _daily_task_ledger (
                        report_date DATE NOT NULL,
                        parser_type VARCHAR(50) NOT NULL,
                        row_key VARCHAR(200) NOT NULL,
                        source VARCHAR(20) NOT NULL,
                        included TINYINT(1) NOT NULL DEFAULT 1,
                        online_present TINYINT(1) NOT NULL DEFAULT 1,
                        community VARCHAR(200) DEFAULT '',
                        inspector VARCHAR(100) DEFAULT '',
                        task_state VARCHAR(20) NOT NULL,
                        unable_to_verify TINYINT(1) NOT NULL DEFAULT 0,
                        reached_bottom TINYINT(1) NOT NULL DEFAULT 0,
                        effective_workload TINYINT UNSIGNED NOT NULL DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (report_date, parser_type, row_key),
                        INDEX idx_ledger_type_date (parser_type, report_date),
                        INDEX idx_ledger_person (report_date, community, inspector)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute(
                    "SHOW COLUMNS FROM _daily_task_ledger "
                    "LIKE 'effective_workload'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "ALTER TABLE _daily_task_ledger "
                        "ADD COLUMN effective_workload "
                        "TINYINT UNSIGNED NOT NULL DEFAULT 0"
                    )
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _daily_task_ledger_runs (
                        report_date DATE NOT NULL,
                        parser_type VARCHAR(50) NOT NULL,
                        snapshot_table VARCHAR(100) NOT NULL,
                        previous_snapshot_table VARCHAR(100) DEFAULT NULL,
                        ledger_rows INT NOT NULL DEFAULT 0,
                        included_rows INT NOT NULL DEFAULT 0,
                        generation_method VARCHAR(20) NOT NULL DEFAULT 'sync',
                        generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (report_date, parser_type),
                        INDEX idx_ledger_run_date (report_date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    COLLATE=utf8mb4_unicode_ci
                """)
        if "registry" in cls._pools:
            async with cls._pools["registry"].acquire() as conn:
                async with conn.cursor() as cur:
                    await ensure_registry_schema(cur)
        if "workflow" in cls._pools:
            async with cls._pools["workflow"].acquire() as conn:
                async with conn.cursor() as cur:
                    await ensure_workflow_schema(cur)
        return cls

    @classmethod
    async def close_all(cls):
        """关闭所有连接池"""
        for pool in cls._pools.values():
            pool.close()
            await pool.wait_closed()
        cls._pools.clear()

    @classmethod
    def get_pool(cls, name: str = "online_data") -> aiomysql.Pool:
        """获取指定数据库的连接池"""
        if name not in cls._pools:
            raise ValueError(f"未知的数据库: {name}，可用: {list(cls._pools.keys())}")
        return cls._pools[name]


# 全局实例
db_manager = DatabaseManager()

# 兼容旧代码
async def init_db():
    # lifespan 中的第一个步骤，业务调度器尚未启动，因此局部 warning 过滤
    # 不会影响运行期数据库请求。
    with suppress_expected_bootstrap_warnings():
        await db_manager.init_all()

async def close_db():
    await db_manager.close_all()


# 依赖注入：默认 online_data 库（兼容现有 Depends(get_db)）
async def get_db():
    """默认获取 OnlineData 库的连接"""
    async with db_manager.get_pool("online_data").acquire() as conn:
        yield conn


def get_db_pool(db_name: str = "online_data"):
    """指定数据库的依赖注入工厂

    用法：conn = Depends(get_db_pool("archive"))
    """
    async def _dependency():
        async with db_manager.get_pool(db_name).acquire() as conn:
            yield conn
    return _dependency
