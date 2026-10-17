"""Reclassify obsolete household issues without deleting evidence or changing properties."""
import json

from services.registry_import import classify_household_file_rows

LEGACY_REASONS = (
    "同一标准化地址存在多条户号表来源行，需人工确认代表记录",
)
REPAIR_NOTE = "household_identity_v2: replaced obsolete preview classification"


async def repair_household_preview(cur, batch_id):
    await cur.execute(
        "SELECT status, imported_count FROM registry_source_batches "
        "WHERE id=%s AND source_type='household' FOR UPDATE", (batch_id,),
    )
    batch = await cur.fetchone()
    if not batch or batch[0] not in {"preview", "partially_imported"}:
        return None
    await cur.execute(
        "SELECT COUNT(*) FROM registry_import_issues WHERE batch_id=%s AND status='pending' "
        "AND issue_type='household_duplicate' AND reason=%s",
        (batch_id, LEGACY_REASONS[0]),
    )
    if not (await cur.fetchone())[0]:
        return None
    await cur.execute(
        "SELECT id, source_ref, payload_json FROM registry_source_records "
        "WHERE batch_id=%s AND entity_type='household_property' ORDER BY id LIMIT 100001",
        (batch_id,),
    )
    records = await cur.fetchall()
    if len(records) > 100000:
        raise RuntimeError("household preview exceeds reclassification limit")
    rows = []
    for _, ref, payload in records:
        row = json.loads(payload) if isinstance(payload, str) else dict(payload)
        row["import_source_ref"] = str(ref)
        rows.append(row)
    classified = classify_household_file_rows(rows)
    await cur.execute(
        "UPDATE registry_import_issues SET status='superseded', review_note=%s "
        "WHERE batch_id=%s AND status='pending' AND issue_type='household_duplicate' AND reason=%s",
        (REPAIR_NOTE, batch_id, LEGACY_REASONS[0]),
    )
    await cur.execute("SELECT source_ref, issue_type FROM registry_import_issues WHERE batch_id=%s "
                      "AND status IN ('pending','resolved','dismissed')", (batch_id,))
    remaining = {(str(ref), str(kind)) for ref, kind in await cur.fetchall()}
    values = [(batch_id, issue["issue_type"], "household", str(issue["payload"]["import_source_ref"]),
               issue["entity_key"], json.dumps(issue["payload"], ensure_ascii=False), issue["reason"])
              for issue in classified["issues"]
              if (str(issue["payload"]["import_source_ref"]), issue["issue_type"]) not in remaining]
    for offset in range(0, len(values), 500):
        await cur.executemany(
            "INSERT INTO registry_import_issues (batch_id,issue_type,source_type,source_ref,entity_key,payload_json,reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)", values[offset:offset + 500],
        )
    await cur.execute(
        "SELECT COUNT(DISTINCT source_ref) FROM registry_import_issues WHERE batch_id=%s AND status='pending'", (batch_id,),
    )
    pending = int((await cur.fetchone())[0])
    candidate = len(rows) - classified["duplicate_row_count"] - pending
    await cur.execute("UPDATE registry_source_batches SET candidate_count=%s, conflict_count=%s WHERE id=%s",
                      (candidate, pending, batch_id))
    return {"batch_id": batch_id, "normal_count": candidate, "issue_count": pending}


async def recover_household_previews():
    """Production startup repair: issue metadata only, no property/source writes."""
    from database import db_manager
    from config import settings
    if settings.APP_ENVIRONMENT != "production":
        return []
    results = []
    async with db_manager.get_pool("registry").acquire() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM registry_source_batches WHERE source_type='household' "
                    "AND status IN ('preview','partially_imported') ORDER BY id LIMIT 100",
                )
                batch_ids = [int(row[0]) for row in await cur.fetchall()]
            for batch_id in batch_ids:
                await conn.begin()
                async with conn.cursor() as cur:
                    result = await repair_household_preview(cur, batch_id)
                await conn.commit()
                if result:
                    results.append(result)
        except Exception:
            await conn.rollback()
            raise
    return results
