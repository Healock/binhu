"""Read-only Tencent Docs monitoring for aggregate online statistics.

This module deliberately does not reuse the retired synchronization switch or
write any Tencent row into the platform business tables.  It keeps only HMAC
digests, community-level counts, and privacy-safe run metadata in the reporting
database.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Any, Iterable

from config import settings
from services.qmf_config import decrypt_secret
from services.business_time import get_business_date
from services.parsers import PARSER_REGISTRY, get_parser


LOCK_NAME = "binhu:txdocs-statistics-monitor"
USAGE_SOURCE = "statistics_monitor"
MONITOR_CONFIG_POLL_SECONDS = 30


def monitoring_environment_allowed() -> bool:
    """Only Production may contact the external Tencent read-only source."""
    return str(settings.APP_ENVIRONMENT or "").strip().lower() == "production"


async def ensure_txdocs_monitor_config_schema(cur) -> None:
    """Create the isolated read-only monitor configuration store."""
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _txdocs_monitor_config (
            id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
            enabled TINYINT(1) NOT NULL DEFAULT 0,
            spreadsheet_url TEXT NOT NULL,
            file_id VARCHAR(200) NOT NULL DEFAULT '',
            data_sheet_id VARCHAR(100) NOT NULL DEFAULT '',
            header_row INT UNSIGNED NOT NULL DEFAULT 1,
            parser_type VARCHAR(50) NOT NULL DEFAULT '',
            client_id VARCHAR(200) NOT NULL DEFAULT '',
            access_token TEXT NOT NULL,
            open_id VARCHAR(200) NOT NULL DEFAULT '',
            interval_seconds INT UNSIGNED NOT NULL DEFAULT 600,
            updated_by INT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _txdocs_monitor_connection (
            id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
            client_id VARCHAR(200) NOT NULL DEFAULT '',
            access_token TEXT NOT NULL,
            open_id VARCHAR(200) NOT NULL DEFAULT '',
            updated_by INT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _txdocs_monitor_target (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            enabled TINYINT(1) NOT NULL DEFAULT 0,
            spreadsheet_url TEXT NOT NULL,
            file_id VARCHAR(200) NOT NULL DEFAULT '',
            data_sheet_id VARCHAR(100) NOT NULL DEFAULT '',
            header_row INT UNSIGNED NOT NULL DEFAULT 1,
            parser_type VARCHAR(50) NOT NULL DEFAULT '',
            interval_seconds INT UNSIGNED NOT NULL DEFAULT 600,
            updated_by INT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_txdocs_monitor_target (file_id, data_sheet_id, parser_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    # PR #718 shipped a singleton config table.  Copy it once into the new
    # shared-connection/target model; the legacy row remains as a rollback
    # reference and is never used when a migrated target exists.
    # Keep this migration compatible with MySQL 8.0.20+, where the legacy
    # VALUES(column) expression is deprecated.  The INSERT only creates the
    # new singleton; the UPDATE fills empty fields without overwriting newer
    # credentials that may have been saved during a rolling deployment.
    await cur.execute(
        """
        INSERT INTO _txdocs_monitor_connection
            (id, client_id, access_token, open_id, updated_by)
        SELECT 1, client_id, access_token, open_id, updated_by
        FROM _txdocs_monitor_config AS legacy
        WHERE legacy.id=1
          AND NOT EXISTS (
              SELECT 1 FROM _txdocs_monitor_connection AS current
              WHERE current.id=1
          )
        """
    )
    await cur.execute(
        """
        UPDATE _txdocs_monitor_connection AS current
        JOIN _txdocs_monitor_config AS legacy ON legacy.id=1
        SET current.client_id=IF(current.client_id='', legacy.client_id, current.client_id),
            current.access_token=IF(current.access_token='', legacy.access_token, current.access_token),
            current.open_id=IF(current.open_id='', legacy.open_id, current.open_id),
            current.updated_by=COALESCE(current.updated_by, legacy.updated_by)
        WHERE current.id=1
        """
    )
    await cur.execute(
        """
        INSERT INTO _txdocs_monitor_target
            (enabled, spreadsheet_url, file_id, data_sheet_id, header_row, parser_type,
             interval_seconds, updated_by)
        SELECT enabled, spreadsheet_url, file_id, data_sheet_id, header_row, parser_type,
               interval_seconds, updated_by
        FROM _txdocs_monitor_config AS legacy
        WHERE legacy.id=1 AND legacy.file_id<>'' AND legacy.data_sheet_id<>''
          AND NOT EXISTS (
              SELECT 1 FROM _txdocs_monitor_target AS target
              WHERE target.file_id=legacy.file_id
                AND target.data_sheet_id=legacy.data_sheet_id
                AND target.parser_type=legacy.parser_type
          )
        """
    )


async def _load_monitor_credentials(cur) -> dict[str, str]:
    await cur.execute(
        """SELECT client_id, access_token, open_id
           FROM _txdocs_monitor_connection WHERE id=1"""
    )
    row = await cur.fetchone()
    if not row:
        # Compatibility with installations that have not run the startup
        # migration yet (and with read-only unit-test cursors).
        await cur.execute(
            """SELECT client_id, access_token, open_id
               FROM _txdocs_monitor_config WHERE id=1"""
        )
        row = await cur.fetchone()
    return {
        "client_id": str(row[0] or "") if row else "",
        "access_token": decrypt_secret(row[1]) if row else "",
        "open_id": str(row[2] or "") if row else "",
    }


async def _load_monitor_targets(
    cur, *, enabled_only: bool = False, include_legacy: bool = False
) -> list[dict[str, Any]]:
    await cur.execute(
        """SELECT id, enabled, spreadsheet_url, file_id, data_sheet_id,
                     header_row, parser_type, interval_seconds, updated_at
                FROM _txdocs_monitor_target
                ORDER BY id"""
    )
    rows = await cur.fetchall()
    targets = [
        {
            "id": int(row[0]), "enabled": bool(row[1]),
            "spreadsheet_url": str(row[2] or ""), "file_id": str(row[3] or ""),
            "sheet_id": str(row[4] or ""), "header_row": int(row[5] or 1),
            "parser_type": str(row[6] or ""),
            "interval_seconds": int(row[7] or 600), "updated_at": row[8],
        }
        for row in rows
        if len(row) >= 9
    ]
    if targets:
        return [target for target in targets if not enabled_only or target["enabled"]]
    if not include_legacy:
        return []
    # Legacy configuration is migration evidence only.  Runtime callers must
    # never reactivate it after the new target table is initialized.
    await cur.execute(
        """SELECT id, enabled, spreadsheet_url, file_id, data_sheet_id,
                     header_row, parser_type, interval_seconds, updated_at
                FROM _txdocs_monitor_config WHERE id=1"""
        + (" AND enabled=1" if enabled_only else "")
    )
    row = await cur.fetchone()
    if not row:
        return []
    return [{
        "id": int(row[0]), "enabled": bool(row[1]),
        "spreadsheet_url": str(row[2] or ""), "file_id": str(row[3] or ""),
        "sheet_id": str(row[4] or ""), "header_row": int(row[5] or 1),
        "parser_type": str(row[6] or ""),
        "interval_seconds": int(row[7] or 600), "updated_at": row[8],
    }]


async def _load_monitor_config(cur) -> dict[str, Any] | None:
    targets = await _load_monitor_targets(cur)
    if not targets:
        return None
    target = dict(targets[0])
    target.update(await _load_monitor_credentials(cur))
    return target


@dataclass(frozen=True)
class MonitorVariant:
    business_key_hash: str
    content_hash: str
    community_hash: str
    community: str


@dataclass(frozen=True)
class MonitorDelta:
    current_total: int
    added: int
    changed: int
    removed: int
    communities: dict[str, dict[str, int]]


def monitoring_spreadsheet_ids(raw: str | None = None) -> tuple[int, ...]:
    """Parse the fixed server-side spreadsheet allowlist."""
    value = settings.TXDOCS_MONITORING_SPREADSHEET_IDS if raw is None else raw
    result: list[int] = []
    for part in str(value or "").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            spreadsheet_id = int(item)
        except ValueError as exc:
            raise ValueError("腾讯只读监控表格白名单必须是逗号分隔的正整数") from exc
        if spreadsheet_id <= 0:
            raise ValueError("腾讯只读监控表格白名单必须是逗号分隔的正整数")
        if spreadsheet_id not in result:
            result.append(spreadsheet_id)
    return tuple(result)


def _hmac_digest(*parts: str) -> str:
    key = settings.registry_hmac_key.encode("utf-8")
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def build_monitor_snapshot(
    spreadsheet_id: int,
    parser_type: str,
    rows: Iterable[dict[str, Any]],
) -> tuple[Counter[MonitorVariant], int]:
    """Convert remote rows to aggregate HMAC variants without retaining text."""
    parser = get_parser(parser_type)
    result: Counter[MonitorVariant] = Counter()
    unkeyed = 0
    for source_row in rows:
        raw_values = source_row.get("values", source_row)
        normalized = parser.normalize_source_row(
            raw_values if isinstance(raw_values, dict) else {}
        )
        ordered_values = tuple(str(normalized.get(column, "") or "") for column in parser.COLUMNS)
        content_hash = _hmac_digest(
            "txdocs-monitor-content",
            str(spreadsheet_id),
            parser_type,
            *ordered_values,
        )
        business_values = tuple(
            str(normalized.get(column, "") or "").strip()
            for column in parser.get_business_key()
        )
        if any(business_values):
            business_key_hash = _hmac_digest(
                "txdocs-monitor-business-key",
                str(spreadsheet_id),
                parser_type,
                *business_values,
            )
        else:
            # Malformed rows still contribute to the external row count.  They
            # cannot be followed across content edits without persisting a
            # physical row or personal text, so a content-derived key is safer.
            unkeyed += 1
            business_key_hash = _hmac_digest(
                "txdocs-monitor-unkeyed",
                str(spreadsheet_id),
                parser_type,
                content_hash,
            )
        community = parser.community_value(normalized)
        community_hash = _hmac_digest("txdocs-monitor-community", community)
        result[
            MonitorVariant(
                business_key_hash=business_key_hash,
                content_hash=content_hash,
                community_hash=community_hash,
                community=community,
            )
        ] += 1
    return result, unkeyed


def _consume(counter: Counter[MonitorVariant], amount: int) -> Counter[MonitorVariant]:
    consumed: Counter[MonitorVariant] = Counter()
    remaining = amount
    for variant in sorted(
        counter,
        key=lambda item: (item.community_hash, item.content_hash),
    ):
        if remaining <= 0:
            break
        take = min(counter[variant], remaining)
        if take:
            consumed[variant] = take
            counter[variant] -= take
            remaining -= take
    return consumed


def compare_monitor_snapshots(
    previous: Counter[MonitorVariant],
    current: Counter[MonitorVariant],
    *,
    has_baseline: bool,
) -> MonitorDelta:
    """Compare multisets while treating row ordering as irrelevant."""
    communities: dict[str, dict[str, int]] = defaultdict(
        lambda: {"current": 0, "added": 0, "changed": 0, "removed": 0}
    )
    for variant, count in current.items():
        communities[variant.community]["current"] += count

    if not has_baseline:
        return MonitorDelta(
            current_total=sum(current.values()),
            added=0,
            changed=0,
            removed=0,
            communities=dict(communities),
        )

    previous_by_key: dict[str, Counter[MonitorVariant]] = defaultdict(Counter)
    current_by_key: dict[str, Counter[MonitorVariant]] = defaultdict(Counter)
    for variant, count in previous.items():
        previous_by_key[variant.business_key_hash][variant] += count
    for variant, count in current.items():
        current_by_key[variant.business_key_hash][variant] += count

    added = changed = removed = 0
    for business_key in set(previous_by_key) | set(current_by_key):
        old = previous_by_key[business_key].copy()
        new = current_by_key[business_key].copy()
        for variant in set(old) & set(new):
            matched = min(old[variant], new[variant])
            old[variant] -= matched
            new[variant] -= matched

        unmatched_old = sum(old.values())
        unmatched_new = sum(new.values())
        changed_count = min(unmatched_old, unmatched_new)
        changed_new = _consume(new, changed_count)
        _consume(old, changed_count)
        for variant, count in changed_new.items():
            communities[variant.community]["changed"] += count
        changed += changed_count

        for variant, count in new.items():
            if count:
                communities[variant.community]["added"] += count
                added += count
        for variant, count in old.items():
            if count:
                communities[variant.community]["removed"] += count
                removed += count

    return MonitorDelta(
        current_total=sum(current.values()),
        added=added,
        changed=changed,
        removed=removed,
        communities=dict(communities),
    )


async def ensure_txdocs_statistics_schema(cur) -> None:
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _txdocs_monitor_current (
            spreadsheet_id INT NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            business_key_hash CHAR(64) NOT NULL,
            content_hash CHAR(64) NOT NULL,
            community_hash CHAR(64) NOT NULL,
            community VARCHAR(200) NOT NULL DEFAULT '',
            row_count INT UNSIGNED NOT NULL,
            last_seen_at DATETIME NOT NULL,
            PRIMARY KEY (
                spreadsheet_id, business_key_hash,
                content_hash, community_hash
            ),
            INDEX idx_txdocs_monitor_current_type (
                parser_type, community_hash
            )
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _txdocs_monitor_runs (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            spreadsheet_id INT NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            observed_date DATE NOT NULL,
            status VARCHAR(20) NOT NULL,
            is_baseline TINYINT(1) NOT NULL DEFAULT 0,
            row_count INT UNSIGNED NOT NULL DEFAULT 0,
            added_count INT UNSIGNED NOT NULL DEFAULT 0,
            changed_count INT UNSIGNED NOT NULL DEFAULT 0,
            removed_count INT UNSIGNED NOT NULL DEFAULT 0,
            unkeyed_count INT UNSIGNED NOT NULL DEFAULT 0,
            error_code VARCHAR(64) NOT NULL DEFAULT '',
            started_at DATETIME NOT NULL,
            finished_at DATETIME NOT NULL,
            INDEX idx_txdocs_monitor_runs_source (
                spreadsheet_id, id
            ),
            INDEX idx_txdocs_monitor_runs_date (
                observed_date, parser_type, status
            )
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _txdocs_monitor_run_communities (
            run_id BIGINT NOT NULL,
            community_hash CHAR(64) NOT NULL,
            community VARCHAR(200) NOT NULL DEFAULT '',
            current_count INT UNSIGNED NOT NULL DEFAULT 0,
            added_count INT UNSIGNED NOT NULL DEFAULT 0,
            changed_count INT UNSIGNED NOT NULL DEFAULT 0,
            removed_count INT UNSIGNED NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, community_hash),
            INDEX idx_txdocs_monitor_run_community (community_hash)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )


async def _load_inputs(
    target_ids: set[int] | None = None,
) -> tuple[dict[str, str] | None, list[dict[str, Any]], Any]:
    from database import db_manager

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            business_date = await get_business_date(cur)
            all_targets = await _load_monitor_targets(
                cur, enabled_only=False, include_legacy=False
            )
            targets = [target for target in all_targets if target["enabled"]]
            if all_targets:
                credentials = await _load_monitor_credentials(cur)
                credentials = credentials if all(credentials.values()) else None
                valid_targets = [
                    target for target in targets
                    if target["file_id"] and target["sheet_id"]
                    and target["parser_type"] in PARSER_REGISTRY
                    and target["parser_type"] != "default"
                    and (target_ids is None or target["id"] in target_ids)
                ]
                return credentials, valid_targets, business_date
            # An initialized but empty target table means that monitoring has
            # no configured sources.  The legacy OAuth/config tables are kept
            # only as migration evidence and must never be reactivated by a
            # rolling deployment or by deleting the last target.
            return None, [], business_date
    finally:
        pool.release(conn)


async def monitoring_configuration_state(cur) -> dict[str, Any]:
    """Return privacy-safe database switch state without exposing credentials."""
    all_targets = await _load_monitor_targets(
        cur, enabled_only=False, include_legacy=False
    )
    targets = [target for target in all_targets if target["enabled"]]
    if not targets:
        return {
            "enabled": False,
            "configured": False,
            "target_count": len(all_targets),
            "enabled_target_count": 0,
            "configured_target_count": 0,
            "credentials_configured": False,
        }
    credentials = await _load_monitor_credentials(cur)
    credentials_configured = bool(
        credentials["client_id"]
        and credentials["access_token"]
        and credentials["open_id"]
    )
    configured_target_count = sum(
        1
        for target in targets
        if (
            target["file_id"] and target["sheet_id"]
            and target["parser_type"] in PARSER_REGISTRY
            and target["parser_type"] != "default"
        )
    )
    return {
        "enabled": True,
        "configured": bool(credentials_configured and configured_target_count),
        "target_count": len(all_targets),
        "enabled_target_count": len(targets),
        "configured_target_count": configured_target_count,
        "credentials_configured": credentials_configured,
    }


async def monitoring_configuration_ready(cur) -> bool:
    """Check the database-controlled target switch and credentials."""
    return bool((await monitoring_configuration_state(cur))["configured"])


async def get_monitoring_runtime_state() -> dict[str, Any]:
    """Load sanitized runtime state without holding a connection during reads."""
    environment_allowed = monitoring_environment_allowed()
    if not environment_allowed:
        return {
            "environment_allowed": False,
            "enabled": False,
            "configured": False,
            "target_count": 0,
            "enabled_target_count": 0,
            "configured_target_count": 0,
            "credentials_configured": False,
        }

    from database import db_manager

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            state = await monitoring_configuration_state(cur)
    finally:
        pool.release(conn)
    return {"environment_allowed": True, **state}


async def _load_previous(cur, spreadsheet_id: int) -> tuple[Counter[MonitorVariant], bool]:
    await cur.execute(
        """
        SELECT business_key_hash, content_hash, community_hash,
               community, row_count
        FROM _txdocs_monitor_current
        WHERE spreadsheet_id=%s
        """,
        (spreadsheet_id,),
    )
    previous: Counter[MonitorVariant] = Counter()
    for row in await cur.fetchall():
        previous[
            MonitorVariant(
                business_key_hash=str(row[0]),
                content_hash=str(row[1]),
                community_hash=str(row[2]),
                community=str(row[3] or ""),
            )
        ] = int(row[4] or 0)
    await cur.execute(
        "SELECT 1 FROM _txdocs_monitor_runs "
        "WHERE spreadsheet_id=%s AND status='success' LIMIT 1",
        (spreadsheet_id,),
    )
    return previous, bool(await cur.fetchone())


async def _persist_success(
    conn,
    config: dict[str, Any],
    business_date,
    started_at: datetime,
    current: Counter[MonitorVariant],
    unkeyed: int,
) -> None:
    async with conn.cursor() as cur:
        await conn.begin()
        try:
            previous, has_baseline = await _load_previous(cur, config["id"])
            delta = compare_monitor_snapshots(
                previous, current, has_baseline=has_baseline
            )
            await cur.execute(
                "DELETE FROM _txdocs_monitor_current WHERE spreadsheet_id=%s",
                (config["id"],),
            )
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if current:
                await cur.executemany(
                    """
                    INSERT INTO _txdocs_monitor_current (
                        spreadsheet_id, parser_type, business_key_hash,
                        content_hash, community_hash, community, row_count,
                        last_seen_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    [
                        (
                            config["id"], config["parser_type"],
                            variant.business_key_hash, variant.content_hash,
                            variant.community_hash, variant.community, count,
                            now,
                        )
                        for variant, count in current.items()
                    ],
                )
            await cur.execute(
                """
                INSERT INTO _txdocs_monitor_runs (
                    spreadsheet_id, parser_type, observed_date, status,
                    is_baseline, row_count, added_count, changed_count,
                    removed_count, unkeyed_count, error_code,
                    started_at, finished_at
                ) VALUES (%s,%s,%s,'success',%s,%s,%s,%s,%s,%s,'',%s,%s)
                """,
                (
                    config["id"], config["parser_type"], business_date,
                    int(not has_baseline), delta.current_total, delta.added,
                    delta.changed, delta.removed, unkeyed, started_at, now,
                ),
            )
            run_id = int(cur.lastrowid)
            if delta.communities:
                await cur.executemany(
                    """
                    INSERT INTO _txdocs_monitor_run_communities (
                        run_id, community_hash, community, current_count,
                        added_count, changed_count, removed_count
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    [
                        (
                            run_id,
                            _hmac_digest("txdocs-monitor-community", community),
                            community,
                            counts["current"], counts["added"],
                            counts["changed"], counts["removed"],
                        )
                        for community, counts in delta.communities.items()
                    ],
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


def _safe_error_code(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "read_timeout"
    if exc.__class__.__name__ == "TxDocsAPIError":
        code = str(getattr(exc, "code", None) or "api_error").strip()
        return f"txdocs_{code}"[:64]
    if isinstance(exc, ValueError):
        return "invalid_sheet_layout"
    return "monitor_failed"


async def _persist_failure(
    conn,
    config: dict[str, Any],
    business_date,
    started_at: datetime,
    code: str,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO _txdocs_monitor_runs (
                spreadsheet_id, parser_type, observed_date, status,
                error_code, started_at, finished_at
            ) VALUES (%s,%s,%s,'failed',%s,%s,UTC_TIMESTAMP())
            """,
            (
                config["id"], config["parser_type"], business_date,
                code[:64], started_at,
            ),
        )
    await conn.commit()


async def _read_config(client: Any, config: dict[str, Any]):
    parser = get_parser(config["parser_type"])
    row_total = await client.get_sheet_row_total(config["file_id"], config["sheet_id"])
    if row_total is not None and row_total > settings.TXDOCS_MONITORING_MAX_ROWS_PER_SHEET:
        raise ValueError("腾讯监控表格超过允许的只读行数")
    columns = await client.resolve_column_layout(
        config["file_id"],
        config["sheet_id"],
        config["header_row"],
        parser.source_column_layouts(),
    )
    rows = await client.read_all_source_rows(
        config["file_id"],
        config["sheet_id"],
        config["header_row"],
        columns,
    )
    if len(rows) > settings.TXDOCS_MONITORING_MAX_ROWS_PER_SHEET:
        raise ValueError("腾讯监控表格超过允许的只读行数")
    return rows


async def get_txdocs_monitor_failure_codes_since(
    started_at: datetime,
) -> tuple[str, ...]:
    """Return privacy-safe failure codes recorded during a monitoring pass."""
    from database import db_manager

    report_pool = db_manager.get_pool("daily_report")
    report_conn = await report_pool.acquire()
    try:
        async with report_conn.cursor() as cur:
            await cur.execute(
                """
                SELECT DISTINCT error_code
                FROM _txdocs_monitor_runs
                WHERE status='failed' AND started_at >= %s AND error_code<>''
                ORDER BY error_code
                """,
                (started_at,),
            )
            return tuple(
                str(row[0]).strip()[:64]
                for row in await cur.fetchall()
                if row and str(row[0] or "").strip()
            )
    finally:
        report_pool.release(report_conn)


async def run_txdocs_statistics_once(
    target_ids: set[int] | None = None,
) -> int:
    """Run one read-only monitoring pass; return successful source count.

    ``target_ids`` is used only by the scheduler to honor each target's own
    interval.  A manual run leaves it unset and reads every enabled target.
    """
    if not monitoring_environment_allowed():
        return 0
    from database import db_manager
    from services.txdocs_client import TxDocsClient

    report_pool = db_manager.get_pool("daily_report")
    report_conn = await report_pool.acquire()
    lock_acquired = False
    client: Any | None = None
    try:
        async with report_conn.cursor() as cur:
            await cur.execute("SELECT GET_LOCK(%s, 0)", (LOCK_NAME,))
            lock_acquired = bool((await cur.fetchone() or [0])[0])
        if not lock_acquired:
            return 0

        credentials, configs, business_date = await _load_inputs(target_ids)
        if not credentials or not business_date:
            print("[TXDOCS_MONITOR] skipped code=credentials_unavailable")
            return 0
        client = TxDocsClient(**credentials, usage_source=USAGE_SOURCE)
        succeeded = 0
        for config in configs:
            started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            try:
                rows = await asyncio.wait_for(
                    _read_config(client, config),
                    timeout=max(10, settings.TXDOCS_MONITORING_SHEET_TIMEOUT_SECONDS),
                )
                snapshot, unkeyed = build_monitor_snapshot(
                    config["id"], config["parser_type"], rows
                )
                await _persist_success(
                    report_conn, config, business_date, started_at,
                    snapshot, unkeyed,
                )
                succeeded += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = _safe_error_code(exc)
                await _persist_failure(
                    report_conn, config, business_date, started_at, code
                )
                print(
                    f"[TXDOCS_MONITOR] source={config['id']} "
                    f"status=failed code={code}"
                )
        return succeeded
    finally:
        if client is not None:
            await client.close()
        if lock_acquired:
            try:
                async with report_conn.cursor() as cur:
                    await cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
            except Exception:
                pass
        report_pool.release(report_conn)


async def run_txdocs_statistics_monitor() -> None:
    """Poll database-controlled targets in Production without restart."""
    if not monitoring_environment_allowed():
        return
    loop = asyncio.get_running_loop()
    next_due: dict[int, float] = {}
    while True:
        try:
            from database import db_manager

            pool = db_manager.get_pool("online_data")
            conn = await pool.acquire()
            try:
                async with conn.cursor() as cur:
                    targets = await _load_monitor_targets(cur, enabled_only=True)
            finally:
                pool.release(conn)
            enabled_ids = {int(target["id"]) for target in targets}
            now = loop.time()
            next_due = {
                target_id: due
                for target_id, due in next_due.items()
                if target_id in enabled_ids
            }
            due_targets: list[dict[str, Any]] = []
            for target in targets:
                target_id = int(target["id"])
                due = next_due.setdefault(target_id, now)
                if due <= now:
                    due_targets.append(target)
            for target in due_targets:
                target_id = int(target["id"])
                await run_txdocs_statistics_once({target_id})
                interval = max(60, min(86400, int(target.get("interval_seconds") or 600)))
                next_due[target_id] = loop.time() + interval
        except asyncio.CancelledError:
            raise
        except Exception:
            print("[TXDOCS_MONITOR] status=failed code=scheduler_iteration_failed")
        now = loop.time()
        waits = [due - now for due in next_due.values() if due > now]
        interval = min(waits, default=MONITOR_CONFIG_POLL_SECONDS)
        await asyncio.sleep(
            max(1, min(interval, MONITOR_CONFIG_POLL_SECONDS))
        )


async def _configured_monitor_source_ids() -> list[int]:
    """Return enabled target IDs; legacy allowlists are migration-only."""
    from database import db_manager

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM _txdocs_monitor_target WHERE enabled=1 ORDER BY id"
            )
            ids = [int(row[0]) for row in await cur.fetchall()]
    except Exception:
        ids = []
    finally:
        pool.release(conn)
    return ids


async def get_txdocs_statistics_overview(
    start_date: str,
    end_date: str,
    parser_types: list[str],
    communities: list[str] | None,
    *,
    configuration_ready: bool | None = None,
    monitoring_enabled: bool | None = None,
    environment_allowed: bool | None = None,
) -> dict[str, Any]:
    """Return aggregate monitoring metrics without exposing remote row text."""
    runtime_allowed = (
        monitoring_environment_allowed()
        if environment_allowed is None
        else bool(environment_allowed)
    )
    database_enabled = (
        bool(configuration_ready)
        if monitoring_enabled is None
        else bool(monitoring_enabled)
    )
    base = {
        "enabled": bool(runtime_allowed and database_enabled),
        "configured": bool(configuration_ready) if configuration_ready is not None else False,
        "status": "disabled",
        "start_date": start_date,
        "end_date": end_date,
        "current_rows": 0,
        "added_rows": 0,
        "changed_rows": 0,
        "removed_rows": 0,
        "successful_reads": 0,
        "failed_sources": 0,
        "last_success_at": None,
        "is_stale": False,
        "message": "腾讯表只读监控未启用",
    }
    if not runtime_allowed:
        return {
            **base,
            "message": "当前环境不允许启用腾讯表只读监控",
        }
    if not database_enabled:
        return base
    if configuration_ready is False:
        return {
            **base,
            "status": "misconfigured",
            "message": "腾讯表只读监控的固定白名单或只读凭据尚未就绪",
        }
    valid_types = [item for item in dict.fromkeys(parser_types) if item in PARSER_REGISTRY]
    if not valid_types:
        return {**base, "status": "unavailable", "message": "当前业务类型没有外部监控数据"}

    type_marks = ", ".join(["%s"] * len(valid_types))
    source_ids = await _configured_monitor_source_ids()
    community_clause = ""
    community_params: list[str] = []
    if communities is not None:
        if not communities:
            return {**base, "status": "healthy", "message": "当前范围没有可见社区"}
        community_marks = ", ".join(["%s"] * len(communities))
        community_clause = f" AND community IN ({community_marks})"
        community_params = communities

    from database import db_manager

    pool = db_manager.get_pool("daily_report")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            if not source_ids:
                return {**base, "status": "healthy", "message": "尚未建立外部成功快照"}
            source_marks = ", ".join(["%s"] * len(source_ids))
            await cur.execute(
                f"""
                SELECT COALESCE(SUM(row_count),0)
                FROM _txdocs_monitor_current
                WHERE parser_type IN ({type_marks})
                  AND spreadsheet_id IN ({source_marks})
                  {community_clause}
                """,
                (*valid_types, *source_ids, *community_params),
            )
            current_rows = int((await cur.fetchone() or [0])[0] or 0)
            await cur.execute(
                f"""
                SELECT COALESCE(SUM(c.added_count),0),
                       COALESCE(SUM(c.changed_count),0),
                       COALESCE(SUM(c.removed_count),0)
                FROM _txdocs_monitor_run_communities AS c
                JOIN _txdocs_monitor_runs AS r ON r.id=c.run_id
                WHERE r.status='success'
                  AND r.is_baseline=0
                  AND r.observed_date BETWEEN %s AND %s
                  AND r.parser_type IN ({type_marks})
                  AND r.spreadsheet_id IN ({source_marks})
                  {community_clause}
                """,
                (
                    start_date, end_date, *valid_types, *source_ids,
                    *community_params,
                ),
            )
            aggregate = await cur.fetchone() or (0, 0, 0)
            await cur.execute(
                f"""
                SELECT COUNT(*)
                FROM _txdocs_monitor_runs
                WHERE status='success'
                  AND observed_date BETWEEN %s AND %s
                  AND parser_type IN ({type_marks})
                  AND spreadsheet_id IN ({source_marks})
                """,
                (start_date, end_date, *valid_types, *source_ids),
            )
            successful_reads = int((await cur.fetchone() or [0])[0] or 0)
            await cur.execute(
                f"""
                SELECT MAX(finished_at), COUNT(*)
                FROM _txdocs_monitor_runs
                WHERE status='success'
                  AND parser_type IN ({type_marks})
                  AND spreadsheet_id IN ({source_marks})
                """,
                (*valid_types, *source_ids),
            )
            success_row = await cur.fetchone() or (None, 0)
            await cur.execute(
                f"""
                SELECT COUNT(*)
                FROM _txdocs_monitor_runs AS latest
                JOIN (
                    SELECT spreadsheet_id, MAX(id) AS id
                    FROM _txdocs_monitor_runs
                    WHERE parser_type IN ({type_marks})
                      AND spreadsheet_id IN ({source_marks})
                    GROUP BY spreadsheet_id
                ) AS selected ON selected.id=latest.id
                WHERE latest.status='failed'
                """,
                (*valid_types, *source_ids),
            )
            failed_sources = int((await cur.fetchone() or [0])[0] or 0)
    finally:
        pool.release(conn)

    last_success = success_row[0]
    if last_success is None:
        return {
            **base,
            "status": "awaiting_first_snapshot",
            "message": "腾讯表只读监控尚未取得首个成功快照",
            "failed_sources": failed_sources,
        }
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_after = max(300, settings.TXDOCS_MONITORING_INTERVAL_SECONDS * 2)
    is_stale = (now - last_success).total_seconds() > stale_after
    status = "error" if failed_sources else ("stale" if is_stale else "healthy")
    message = {
        "healthy": "腾讯表只读监控正常",
        "stale": "腾讯表监控数据已超过预期刷新时间，当前保留最后成功结果",
        "error": "部分腾讯表读取失败，当前保留最后成功结果",
    }[status]
    return {
        **base,
        "status": status,
        "current_rows": current_rows,
        "added_rows": int(aggregate[0] or 0),
        "changed_rows": int(aggregate[1] or 0),
        "removed_rows": int(aggregate[2] or 0),
        "successful_reads": successful_reads,
        "failed_sources": failed_sources,
        "last_success_at": last_success.isoformat() + "Z",
        "is_stale": is_stale,
        "message": message,
    }
