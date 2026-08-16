"""Bundled administrative-area reference data used by QMF registration.

The source CSV is public administrative-division metadata.  It is packaged
with the application and imported idempotently into PlatformData; no resident
or task data is stored in this table.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


AREA_DATA_PATH = Path(__file__).resolve().parent.parent / "assets" / "areas.csv"
AREA_DATA_CONFIG_KEY = "administrative_areas_sha256"
AREA_DATA_COLUMNS = (
    "code",
    "name",
    "level",
    "province",
    "city",
    "parent_code",
    "path",
    "status",
    "start_year",
    "end_year",
    "new_code",
    "source",
)


@dataclass(frozen=True)
class AdministrativeArea:
    source_row: int
    code: str
    name: str
    level: str
    province: str
    city: str
    parent_code: str
    path: str
    full_name: str
    status: str
    start_year: int | None
    end_year: int | None
    new_code: str
    source: str


def _year(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    year = int(text)
    if year < 1900 or year > 2200:
        raise ValueError("administrative area year is out of range")
    return year


def _full_name(row: dict[str, str]) -> str:
    path = str(row.get("path") or "").strip()
    if path:
        return path.replace("/", "").replace("\\", "")
    parts: list[str] = []
    for value in (row.get("province"), row.get("city"), row.get("name")):
        text = str(value or "").strip()
        if text and text != "直辖" and text not in parts:
            parts.append(text)
    return "".join(parts)


def load_administrative_areas(
    path: Path = AREA_DATA_PATH,
) -> tuple[str, list[AdministrativeArea]]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    rows: list[AdministrativeArea] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != AREA_DATA_COLUMNS:
            raise ValueError("administrative area CSV columns do not match the contract")
        for source_row, item in enumerate(reader, start=2):
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or "").strip()
            if len(code) != 6 or not code.isdigit() or not name:
                raise ValueError(f"invalid administrative area row {source_row}")
            parent_code = str(item.get("parent_code") or "").strip()
            new_code = str(item.get("new_code") or "").strip()
            if parent_code and (len(parent_code) != 6 or not parent_code.isdigit()):
                raise ValueError(f"invalid parent code at row {source_row}")
            if len(new_code) > 300:
                raise ValueError(f"replacement metadata is too long at row {source_row}")
            full_name = _full_name(item)
            if not full_name:
                raise ValueError(f"administrative area path is empty at row {source_row}")
            rows.append(
                AdministrativeArea(
                    source_row=source_row,
                    code=code,
                    name=name,
                    level=str(item.get("level") or "").strip(),
                    province=str(item.get("province") or "").strip(),
                    city=str(item.get("city") or "").strip(),
                    parent_code=parent_code,
                    path=str(item.get("path") or "").strip(),
                    full_name=full_name,
                    status=str(item.get("status") or "").strip(),
                    start_year=_year(item.get("start_year")),
                    end_year=_year(item.get("end_year")),
                    new_code=new_code,
                    source=str(item.get("source") or "").strip(),
                )
            )
    if not rows:
        raise ValueError("administrative area CSV is empty")
    return digest, rows


def choose_administrative_area(
    rows: Iterable[AdministrativeArea],
    *,
    birth_year: int,
) -> AdministrativeArea | None:
    candidates = list(rows)
    if not candidates:
        return None

    def rank(item: AdministrativeArea) -> tuple[int, int, int, int]:
        covers_birth = (
            (item.start_year is None or item.start_year <= birth_year)
            and (item.end_year is None or item.end_year >= birth_year)
        )
        return (
            0 if covers_birth else 1,
            0 if item.status == "active" else 1,
            -(item.end_year or 9999),
            -(item.start_year or 0),
        )

    return min(candidates, key=rank)


async def resolve_identity_area(cur, identity: str) -> AdministrativeArea | None:
    code = identity[:6]
    birth_year = int(identity[6:10])
    await cur.execute(
        """
        SELECT source_row, code, name, level, province, city, parent_code,
               path, full_name, status, start_year, end_year, new_code, source
        FROM _administrative_areas
        WHERE code=%s
        ORDER BY source_row
        """,
        (code,),
    )
    rows = [AdministrativeArea(*row) for row in await cur.fetchall()]
    return choose_administrative_area(rows, birth_year=birth_year)


async def ensure_administrative_area_schema(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _administrative_areas (
            source_row INT NOT NULL PRIMARY KEY,
            code CHAR(6) NOT NULL,
            name VARCHAR(100) NOT NULL,
            level VARCHAR(20) NOT NULL,
            province VARCHAR(100) NOT NULL DEFAULT '',
            city VARCHAR(100) NOT NULL DEFAULT '',
            parent_code CHAR(6) NOT NULL DEFAULT '',
            path VARCHAR(300) NOT NULL DEFAULT '',
            full_name VARCHAR(300) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT '',
            start_year SMALLINT DEFAULT NULL,
            end_year SMALLINT DEFAULT NULL,
            new_code VARCHAR(300) NOT NULL DEFAULT '',
            source VARCHAR(50) NOT NULL DEFAULT '',
            INDEX idx_administrative_area_code (code, status),
            INDEX idx_administrative_area_period (code, start_year, end_year)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    digest, rows = load_administrative_areas()
    await cur.execute(
        "SELECT config_value FROM _system_config WHERE config_key=%s",
        (AREA_DATA_CONFIG_KEY,),
    )
    stored = await cur.fetchone()
    await cur.execute("SELECT COUNT(*) FROM _administrative_areas")
    count_row = await cur.fetchone()
    if stored and str(stored[0] or "") == digest and int(count_row[0] or 0) == len(rows):
        return

    payload = [
        (
            item.source_row,
            item.code,
            item.name,
            item.level,
            item.province,
            item.city,
            item.parent_code,
            item.path,
            item.full_name,
            item.status,
            item.start_year,
            item.end_year,
            item.new_code,
            item.source,
        )
        for item in rows
    ]
    await cur.execute("START TRANSACTION")
    try:
        await cur.execute("DELETE FROM _administrative_areas")
        for offset in range(0, len(payload), 500):
            await cur.executemany(
                """
                INSERT INTO _administrative_areas (
                    source_row, code, name, level, province, city, parent_code,
                    path, full_name, status, start_year, end_year, new_code, source
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                payload[offset : offset + 500],
            )
        await cur.execute(
            """
            INSERT INTO _system_config (config_key, config_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE config_value=VALUES(config_value)
            """,
            (AREA_DATA_CONFIG_KEY, digest),
        )
        await cur.execute("COMMIT")
    except Exception:
        await cur.execute("ROLLBACK")
        raise
