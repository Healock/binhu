"""Load declarative process definitions without process-specific branches."""

from __future__ import annotations

import json
from typing import Any

from services.process_engine import ProcessValidationError, validate_form_schema, validate_nodes


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProcessValidationError(f"流程定义缺少 {field}")
    return text


async def load_process_bundle(cur, bundle: dict[str, Any], *, actor_user_id: int | None = None) -> dict[str, Any]:
    """Upsert one form/process pair and publish only changed versions.

    The bundle contains only generic form and ordered-node data. Running the
    loader repeatedly with the same JSON does not create another version.
    """
    if not isinstance(bundle, dict):
        raise ProcessValidationError("流程定义文件必须是 JSON 对象")
    form = bundle.get("form")
    process = bundle.get("process")
    if not isinstance(form, dict) or not isinstance(process, dict):
        raise ProcessValidationError("流程定义文件必须包含 form 和 process")
    form_code = _require_text(form.get("code"), "form.code")
    process_code = _require_text(process.get("code"), "process.code")
    schema = form.get("schema")
    validate_form_schema(schema)
    nodes = validate_nodes(process.get("nodes"))

    await cur.execute(
        "SELECT id,current_version_id FROM form_definitions WHERE code=%s FOR UPDATE",
        (form_code,),
    )
    form_row = await cur.fetchone()
    if form_row:
        form_id, current_form_version_id = int(form_row[0]), form_row[1]
        await cur.execute(
            "UPDATE form_definitions SET name=%s,description=%s,updated_by=%s WHERE id=%s",
            (str(form.get("name") or form_code), str(form.get("description") or ""), actor_user_id, form_id),
        )
    else:
        await cur.execute(
            "INSERT INTO form_definitions (code,name,description,enabled,created_by,updated_by) "
            "VALUES (%s,%s,%s,0,%s,%s)",
            (form_code, str(form.get("name") or form_code), str(form.get("description") or ""), actor_user_id, actor_user_id),
        )
        form_id, current_form_version_id = int(cur.lastrowid), None

    form_version_changed = True
    if current_form_version_id:
        await cur.execute("SELECT schema_json,status FROM form_versions WHERE id=%s", (current_form_version_id,))
        current = await cur.fetchone()
        form_version_changed = not current or str(current[1]) != "published" or _canonical(json.loads(current[0]) if isinstance(current[0], str) else current[0]) != _canonical(schema)
    if form_version_changed:
        await cur.execute(
            "SELECT COALESCE(MAX(version_no),0) FROM form_versions WHERE form_definition_id=%s",
            (form_id,),
        )
        version_no = int((await cur.fetchone())[0]) + 1
        await cur.execute(
            "INSERT INTO form_versions (form_definition_id,version_no,schema_json,status,published_by,published_at) "
            "VALUES (%s,%s,%s,'published',%s,UTC_TIMESTAMP())",
            (form_id, version_no, _canonical(schema), actor_user_id),
        )
        current_form_version_id = int(cur.lastrowid)
        await cur.execute(
            "UPDATE form_definitions SET current_version_id=%s,enabled=1,updated_by=%s WHERE id=%s",
            (current_form_version_id, actor_user_id, form_id),
        )

    await cur.execute(
        "SELECT id,current_version_id FROM process_definitions WHERE code=%s FOR UPDATE",
        (process_code,),
    )
    process_row = await cur.fetchone()
    if process_row:
        process_id, current_process_version_id = int(process_row[0]), process_row[1]
        await cur.execute(
            "UPDATE process_definitions SET name=%s,description=%s,form_definition_id=%s,module_id=%s,"
            "default_due_hours=%s,updated_by=%s WHERE id=%s",
            (str(process.get("name") or process_code), str(process.get("description") or ""), form_id,
             str(process.get("module_id") or "workflow"), process.get("default_due_hours"), actor_user_id, process_id),
        )
    else:
        await cur.execute(
            "INSERT INTO process_definitions "
            "(code,name,description,form_definition_id,module_id,default_due_hours,enabled,created_by,updated_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s)",
            (process_code, str(process.get("name") or process_code), str(process.get("description") or ""), form_id,
             str(process.get("module_id") or "workflow"), process.get("default_due_hours"), actor_user_id, actor_user_id),
        )
        process_id, current_process_version_id = int(cur.lastrowid), None

    process_version_changed = True
    if current_process_version_id:
        await cur.execute("SELECT nodes_json,status FROM process_versions WHERE id=%s", (current_process_version_id,))
        current = await cur.fetchone()
        process_version_changed = not current or str(current[1]) != "published" or _canonical(json.loads(current[0]) if isinstance(current[0], str) else current[0]) != _canonical(nodes)
    if process_version_changed:
        await cur.execute(
            "SELECT COALESCE(MAX(version_no),0) FROM process_versions WHERE process_definition_id=%s",
            (process_id,),
        )
        version_no = int((await cur.fetchone())[0]) + 1
        await cur.execute(
            "INSERT INTO process_versions (process_definition_id,version_no,nodes_json,status,published_by,published_at) "
            "VALUES (%s,%s,%s,'published',%s,UTC_TIMESTAMP())",
            (process_id, version_no, _canonical(nodes), actor_user_id),
        )
        current_process_version_id = int(cur.lastrowid)
        await cur.execute(
            "UPDATE process_definitions SET current_version_id=%s,enabled=1,updated_by=%s WHERE id=%s",
            (current_process_version_id, actor_user_id, process_id),
        )

    return {
        "form_definition_id": form_id,
        "form_version_id": int(current_form_version_id),
        "form_version_changed": form_version_changed,
        "process_definition_id": process_id,
        "process_version_id": int(current_process_version_id),
        "process_version_changed": process_version_changed,
    }
