"""Small helpers for warning-free, idempotent report-table DDL."""


async def table_exists(cur, schema: str, table_name: str) -> bool:
    """Return whether a table exists without issuing warning-producing DDL."""

    await cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name=%s LIMIT 1",
        (schema, table_name),
    )
    return await cur.fetchone() is not None
