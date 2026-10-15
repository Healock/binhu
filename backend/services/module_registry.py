"""平台功能模块注册表、部门绑定与访问判定。

模块归属是现有权限之外的一层门禁。模块绑定真实的部门 ID，人员所属
部门沿 ``_departments.parent_id`` 向上展开后与绑定集合求交；部门类型只
用于描述部门本身，不参与模块授权判断。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from services.permissions import has_permission


MODULE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "module_id": "common", "name": "公共功能", "is_public": True,
        "required_permissions": [], "route": "/", "mobile_visible": True,
        "default_department_names": [],
    },
    {
        "module_id": "community_police", "name": "社区警务", "is_public": False,
        "required_permissions": [],
        "route": "/query,/tasks,/address-confirmation,/police-tasks,/police-analysis,/summary,/visit-summary,/code-summary,/data-upload,/work-log,/police-addresses,/qr-codes,/registry",
        "mobile_visible": True,
        # PR-A0.5/上线清单可按业务需要再给综合指挥室追加绑定；初始
        # 只绑定社区警务队，避免未验收账号突然获得全部社区业务。
        "default_department_names": ["社区警务队"],
    },
    {
        "module_id": "workflow", "name": "工单中心", "is_public": False,
        "required_permissions": ["workflow.ticket.view"],
        "route": "/workflow", "mobile_visible": True,
        "default_department_names": ["社区警务队"],
    },
    {
        "module_id": "platform_admin", "name": "平台管理", "is_public": False,
        "required_permissions": [],
        "route": "/users,/permission-groups,/operations,/settings/system,/settings/server-mac,/settings/workflow,/settings/function-modules,/settings/txdocs-monitor,/task-flow-lab,/task-flow-rete-lab",
        "mobile_visible": True,
        # 平台管理由 admin/super_admin 特殊规则开放，不绑定“内勤”部门。
        "default_department_names": [],
    },
)


def _admin_access(user: dict[str, Any]) -> bool:
    if str(user.get("role") or "") in {"admin", "super_admin"}:
        return True
    return any(
        str(group.get("code") or "") in {"admin", "super_admin"}
        for group in user.get("permission_groups") or []
        if isinstance(group, dict)
    )


def _user_department_ids(user: dict[str, Any]) -> set[int]:
    result: set[int] = set()
    for item in user.get("departments") or []:
        if not isinstance(item, dict):
            continue
        try:
            value = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            result.add(value)
    return result


def _department_ancestors(
    department_ids: Iterable[int],
    parent_ids: dict[int, int | None],
) -> set[int]:
    """Return each selected department and every available ancestor."""
    expanded: set[int] = set()
    for raw_id in department_ids:
        try:
            current: int | None = int(raw_id)
        except (TypeError, ValueError):
            continue
        seen: set[int] = set()
        while current and current not in seen:
            seen.add(current)
            expanded.add(current)
            parent = parent_ids.get(current)
            current = int(parent) if parent is not None else None
    return expanded


def _has_department_access(user: dict[str, Any], definition: dict[str, Any]) -> bool:
    granted = {
        int(value)
        for value in definition.get("department_ids") or []
        if str(value).strip().isdigit() and int(value) > 0
    }
    if not granted:
        return False
    parent_ids: dict[int, int | None] = {}
    for key, value in (
        user.get("department_parent_ids")
        or definition.get("_department_parent_ids")
        or {}
    ).items():
        try:
            parent_ids[int(key)] = int(value) if value is not None else None
        except (TypeError, ValueError):
            continue
    for item in user.get("departments") or []:
        if isinstance(item, dict) and item.get("id") is not None:
            try:
                # Legacy member queries omit parent_id and serialize it as
                # None. They must not erase the complete registry tree.
                parent_ids.setdefault(int(item["id"]), (
                    int(item["parent_id"])
                    if item.get("parent_id") is not None else None
                ))
            except (TypeError, ValueError):
                continue
    return bool(_department_ancestors(_user_department_ids(user), parent_ids) & granted)


def _required_permissions(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = []
    return [str(item) for item in value or [] if str(item).strip()]


def route_prefixes(value: Any) -> list[str]:
    """Normalize the stored route field into site-local path prefixes."""
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = str(value or "").replace("\n", ",").split(",")
    return sorted({str(item).strip().rstrip("/") or "/" for item in parts if str(item).strip()})


def can_access_definition(user: dict[str, Any], definition: dict[str, Any]) -> bool:
    """Apply enabled -> administrator -> public/department -> permissions order."""
    if not definition.get("enabled", True):
        return False
    if not _admin_access(user) and not definition.get("is_public") and not _has_department_access(user, definition):
        return False
    return all(
        has_permission(user, permission)
        for permission in _required_permissions(definition.get("required_permissions"))
    )


def available_module_ids(user: dict[str, Any], definitions: Iterable[dict[str, Any]] | None = None) -> list[str]:
    source = definitions if definitions is not None else MODULE_DEFINITIONS
    return sorted(str(item["module_id"]) for item in source if can_access_definition(user, item))


def available_module_routes(
    user: dict[str, Any], definitions: Iterable[dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    source = definitions if definitions is not None else MODULE_DEFINITIONS
    return {
        str(item["module_id"]): route_prefixes(item.get("route"))
        for item in source
        if can_access_definition(user, item)
    }


def available_mobile_module_ids(
    user: dict[str, Any], definitions: Iterable[dict[str, Any]] | None = None,
) -> list[str]:
    source = definitions if definitions is not None else MODULE_DEFINITIONS
    return sorted(
        str(item["module_id"])
        for item in source
        if item.get("mobile_visible", True) and can_access_definition(user, item)
    )


async def _load_department_tree(cur) -> tuple[dict[int, int | None], dict[int, dict[str, Any]]]:
    try:
        await cur.execute(
            "SELECT id, name, parent_id, department_type, is_active FROM _departments"
        )
        rows = await cur.fetchall()
    except Exception:
        # PR-A is intentionally dependent on PR-A0.  Until its parent_id
        # migration is present, fail closed for ordinary users while keeping
        # authentication/bootstrap available for the rollout checklist.
        await cur.execute(
            "SELECT id, name, department_type, is_active FROM _departments"
        )
        rows = [(row[0], row[1], None, row[2], row[3]) for row in await cur.fetchall()]
    parent_ids: dict[int, int | None] = {}
    details: dict[int, dict[str, Any]] = {}
    for row in rows:
        department_id = int(row[0])
        # Compatibility with pre-PR-A0 test/legacy cursors that return the
        # old four-column shape (id, name, type, active).
        has_parent_column = len(row) >= 5 and (
            row[2] is None or isinstance(row[2], int) or str(row[2]).strip().isdigit()
        )
        parent_ids[department_id] = (
            int(row[2]) if has_parent_column and row[2] is not None else None
        )
        type_index = 3 if has_parent_column else 2
        active_index = 4 if has_parent_column else 3
        details[department_id] = {
            "id": department_id,
            "name": str(row[1] or ""),
            "parent_id": parent_ids[department_id],
            "type": str(row[type_index] or ""),
            "is_active": bool(row[active_index]),
        }
    return parent_ids, details


async def load_module_definitions(cur) -> list[dict[str, Any]]:
    await cur.execute(
        "SELECT module.module_id, module.name, module.is_public, "
        "module.required_permissions, module.route, module.mobile_visible, "
        "module.enabled, module.version, unit.department_id "
        "FROM _function_modules AS module "
        "LEFT JOIN _function_module_units AS unit "
        "ON unit.module_id=module.module_id "
        "ORDER BY module.module_id, unit.department_id"
    )
    rows = await cur.fetchall()
    parent_ids, department_details = await _load_department_tree(cur)
    definitions: dict[str, dict[str, Any]] = {}
    for row in rows:
        if len(row) < 9:
            continue
        module_id = str(row[0])
        item = definitions.setdefault(module_id, {
            "module_id": module_id,
            "name": str(row[1] or ""),
            "is_public": bool(row[2]),
            "required_permissions": _required_permissions(row[3]),
            "route": str(row[4] or ""),
            "routes": route_prefixes(row[4]),
            "mobile_visible": bool(row[5]),
            "enabled": bool(row[6]),
            "version": int(row[7] or 1),
            "department_ids": [],
            "departments": [],
            "_department_parent_ids": parent_ids,
        })
        if row[8] is not None:
            department_id = int(row[8])
            if department_id not in item["department_ids"]:
                item["department_ids"].append(department_id)
                if department_id in department_details:
                    item["departments"].append(department_details[department_id])
    return list(definitions.values())


async def available_module_ids_from_cursor(user: dict[str, Any], cur) -> list[str]:
    return available_module_ids(user, await load_module_definitions(cur))


async def available_module_routes_from_cursor(user: dict[str, Any], cur) -> dict[str, list[str]]:
    return available_module_routes(user, await load_module_definitions(cur))


async def available_mobile_module_ids_from_cursor(user: dict[str, Any], cur) -> list[str]:
    return available_mobile_module_ids(user, await load_module_definitions(cur))


async def ensure_function_module_schema(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _function_modules (
            module_id VARCHAR(60) NOT NULL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            is_public TINYINT(1) NOT NULL DEFAULT 0,
            required_permissions JSON NOT NULL,
            route VARCHAR(190) NOT NULL DEFAULT '',
            mobile_visible TINYINT(1) NOT NULL DEFAULT 1,
            enabled TINYINT(1) NOT NULL DEFAULT 1,
            version INT UNSIGNED NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_function_module_public (is_public, enabled)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    # Development databases may have been initialized from the old draft PR.
    # Remove only its obsolete ownership column; historical snapshots stay intact.
    await cur.execute("SHOW COLUMNS FROM _function_modules LIKE 'owner_unit'")
    if await cur.fetchone():
        await cur.execute("SHOW INDEX FROM _function_modules WHERE Key_name='idx_function_module_owner'")
        if await cur.fetchone():
            await cur.execute("ALTER TABLE _function_modules DROP INDEX idx_function_module_owner")
        await cur.execute("ALTER TABLE _function_modules DROP COLUMN owner_unit")
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _function_module_units (
            module_id VARCHAR(60) NOT NULL,
            department_id INT NOT NULL,
            granted_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (module_id, department_id),
            INDEX idx_function_module_unit_department (department_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _function_module_versions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            module_id VARCHAR(60) NOT NULL,
            version INT UNSIGNED NOT NULL,
            snapshot JSON NOT NULL,
            changed_by BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_function_module_version (module_id, version)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    # This startup-only function runs on the autocommit bootstrap connection.
    # DDL above implicitly commits in MySQL, so start the seed transaction only
    # afterwards. The persistent marker row serializes concurrent initializers;
    # its lock is held until the bindings AND snapshots have committed.
    await cur.execute("START TRANSACTION")
    try:
        await cur.execute(
            "INSERT INTO _system_config (config_key, config_value) "
            "VALUES ('function_module_units_seeded', '0') "
            "ON DUPLICATE KEY UPDATE config_key=config_key"
        )
        await cur.execute(
            "SELECT config_value FROM _system_config "
            "WHERE config_key='function_module_units_seeded' FOR UPDATE"
        )
        marker = await cur.fetchone()
        if not marker or str(marker[0]) not in {"0", "1"}:
            raise RuntimeError("功能模块种子标记无效，请管理员核对配置")
        await _initialize_function_modules(cur, seed_units=str(marker[0]) == "0")
        await cur.execute(
            "UPDATE _system_config SET config_value='1' "
            "WHERE config_key='function_module_units_seeded'"
        )
        await cur.execute("COMMIT")
    except BaseException:
        await cur.execute("ROLLBACK")
        raise


async def _initialize_function_modules(cur, *, seed_units: bool) -> None:
    for definition in MODULE_DEFINITIONS:
        module_id = definition["module_id"]
        await cur.execute(
            "SELECT name,is_public,required_permissions,route,mobile_visible,enabled,version "
            "FROM _function_modules WHERE module_id=%s FOR UPDATE",
            (module_id,),
        )
        current = await cur.fetchone()
        is_new = current is None
        if is_new:
            await cur.execute(
                "INSERT INTO _function_modules "
                "(module_id, name, is_public, required_permissions, route, mobile_visible, enabled, version) "
                "VALUES (%s,%s,%s,%s,%s,%s,1,1)",
                (
                    module_id, definition["name"], int(definition["is_public"]),
                    json.dumps(definition["required_permissions"], ensure_ascii=False),
                    definition["route"], int(definition["mobile_visible"]),
                ),
            )
            current = (
                definition["name"], int(definition["is_public"]),
                json.dumps(definition["required_permissions"]), definition["route"],
                int(definition["mobile_visible"]), 1, 1,
            )
        version = int(current[6] or 1)
        await cur.execute(
            "SELECT snapshot FROM _function_module_versions WHERE module_id=%s AND version=%s FOR UPDATE",
            (module_id, version),
        )
        previous = await cur.fetchone()
        previous_snapshot = json.loads(previous[0]) if previous and previous[0] else {}
        is_legacy = "owner_unit" in previous_snapshot
        # An existing modern module is already configured, even with no units
        # and no marker (upgrade from the previous draft). Never infer that an
        # empty binding means permission to restore defaults.
        names = list(definition.get("default_department_names") or [])
        if seed_units and (is_new or is_legacy) and names:
            placeholders = ", ".join(["%s"] * len(names))
            await cur.execute(
                f"SELECT id FROM _departments WHERE name IN ({placeholders}) AND is_active=1 ORDER BY id",
                names,
            )
            department_ids = [int(row[0]) for row in await cur.fetchall()]
            if not department_ids:
                raise RuntimeError("功能模块默认归属部门尚未就绪，请先完成 PR-A0 部门迁移")
            for department_id in department_ids:
                await cur.execute(
                    "INSERT IGNORE INTO _function_module_units "
                    "(module_id, department_id) VALUES (%s,%s)",
                    (definition["module_id"], department_id),
                )
        await cur.execute(
            "SELECT department_id FROM _function_module_units WHERE module_id=%s ORDER BY department_id FOR UPDATE",
            (module_id,),
        )
        department_ids = [int(row[0]) for row in await cur.fetchall()]
        if is_legacy:
            version += 1
            await cur.execute(
                "UPDATE _function_modules SET version=%s WHERE module_id=%s",
                (version, module_id),
            )
        if not previous or is_legacy:
            snapshot_definition = {
                "module_id": module_id, "name": current[0], "is_public": bool(current[1]),
                "required_permissions": _required_permissions(current[2]), "route": current[3],
                "mobile_visible": bool(current[4]), "enabled": bool(current[5]),
                "version": version, "department_ids": department_ids,
            }
            await cur.execute(
                "INSERT INTO _function_module_versions "
                "(module_id, version, snapshot) VALUES (%s, %s, %s)",
                (
                    module_id, version,
                    json.dumps(snapshot_definition, ensure_ascii=False, separators=(",", ":")),
                ),
            )
