"""Startup backfill for the indexed assignment projection fields."""

from __future__ import annotations

import json

from services.task_workflow import TASK_WORKFLOWS


LOCK_NAME = "binhu_assignment_projection_backfill"
BATCH_SIZE = 500


async def ensure_assignment_projection_backfill_schema(cur) -> None:
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _assignment_projection_backfill (
            id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            last_parser_type VARCHAR(50) NOT NULL DEFAULT '',
            last_row_key CHAR(32) NOT NULL DEFAULT '',
            processed_count INT UNSIGNED NOT NULL DEFAULT 0,
            started_at DATETIME DEFAULT NULL,
            completed_at DATETIME DEFAULT NULL,
            error_code VARCHAR(100) NOT NULL DEFAULT '',
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    await cur.execute(
        "INSERT IGNORE INTO _assignment_projection_backfill (id) VALUES (1)"
    )


def _values(raw) -> dict[str, str]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


async def run_assignment_projection_backfill(conn) -> dict[str, int | str]:
    """Populate assignment fields in resumable, 500-row transactions.

    A named lock makes concurrent API/worker processes harmless.  Any database
    failure is propagated to the caller so startup cannot expose a half-ready
    assignment workbench.
    """
    async with conn.cursor() as cur:
        await ensure_assignment_projection_backfill_schema(cur)
        # Startup must not expose a partially populated projection while a
        # sibling process is completing the same migration.  Wait briefly for
        # the owner instead of returning a successful-but-incomplete status.
        await cur.execute("SELECT GET_LOCK(%s, 60)", (LOCK_NAME,))
        lock_row = await cur.fetchone()
        if not lock_row or int(lock_row[0] or 0) != 1:
            raise RuntimeError("assignment projection backfill lock unavailable")
        try:
            await cur.execute(
                "SELECT status,last_parser_type,last_row_key,processed_count "
                "FROM _assignment_projection_backfill WHERE id=1"
            )
            state = await cur.fetchone()
            if state and str(state[0] or "") == "completed":
                return {"status": "completed", "processed": int(state[3] or 0)}
            last_parser = str(state[1] or "") if state else ""
            last_key = str(state[2] or "") if state else ""
            processed = int(state[3] or 0) if state else 0
            await cur.execute(
                "UPDATE _assignment_projection_backfill SET status='running', "
                "started_at=COALESCE(started_at,UTC_TIMESTAMP()), error_code='' "
                "WHERE id=1"
            )
            while True:
                await cur.execute(
                    """
                    SELECT parser_type,row_key,values_json,community,source_count,
                           conflict,task_state,inspector
                    FROM _online_source_projection
                    WHERE (parser_type>%s OR (parser_type=%s AND row_key>%s))
                    ORDER BY parser_type,row_key
                    LIMIT %s
                    """,
                    (last_parser, last_parser, last_key, BATCH_SIZE),
                )
                rows = await cur.fetchall()
                if not rows:
                    await cur.execute(
                        "UPDATE _assignment_projection_backfill SET status='completed', "
                        "completed_at=UTC_TIMESTAMP(), updated_at=UTC_TIMESTAMP() WHERE id=1"
                    )
                    return {"status": "completed", "processed": processed}
                from services.online_source import assignment_projection_fields

                updates = []
                for parser_type, row_key, raw_values, community, source_count, conflict, task_state, inspector in rows:
                    values = _values(raw_values)
                    if str(parser_type) not in TASK_WORKFLOWS:
                        last_parser = str(parser_type)
                        last_key = str(row_key)
                        continue
                    source_label, address, sort_key, queue_ready = assignment_projection_fields(
                        str(parser_type), values, community=str(community or ""),
                        source_count=int(source_count or 0), conflict=bool(conflict),
                        task_state_value=str(task_state or ""),
                    )
                    updates.append((source_label, address, sort_key, queue_ready, parser_type, row_key))
                await conn.begin()
                if updates:
                    await cur.executemany(
                        """
                        UPDATE _online_source_projection
                        SET assignment_source_label=%s,
                            assignment_address_display=%s,
                            assignment_address_sort_key=%s,
                            assignment_queue_ready=%s
                        WHERE parser_type=%s AND row_key=%s
                        """,
                        updates,
                    )
                last_parser = str(rows[-1][0])
                last_key = str(rows[-1][1])
                processed += len(rows)
                await cur.execute(
                    "UPDATE _assignment_projection_backfill SET last_parser_type=%s, "
                    "last_row_key=%s, processed_count=%s WHERE id=1",
                    (last_parser, last_key, processed),
                )
                await conn.commit()
        except Exception:
            try:
                await conn.rollback()
                await cur.execute(
                    "UPDATE _assignment_projection_backfill SET status='failed', "
                    "error_code='backfill_failed' WHERE id=1"
                )
            except Exception:
                pass
            raise
        finally:
            await cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
            await cur.fetchone()
