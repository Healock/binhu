from __future__ import annotations

import warnings
import os

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from database import _ensure_varchar_length, suppress_expected_bootstrap_warnings


class VarcharCursor:
    def __init__(self, column_type: str | None):
        self.column_type = column_type
        self.statements: list[tuple[str, tuple | None]] = []

    async def execute(self, sql, params=None):
        self.statements.append((sql, params))

    async def fetchone(self):
        if self.column_type is None:
            return None
        return ("电话号码", self.column_type, "YES", "", None, "")


def test_bootstrap_warning_filter_only_hides_known_idempotent_messages():
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with suppress_expected_bootstrap_warnings():
            warnings.warn("Table 'PlatformData._users' already exists", Warning)
            warnings.warn(
                "Duplicate entry 'synthetic' for key '_system_config.PRIMARY'",
                Warning,
            )
            warnings.warn("Unknown table 'daily_report.synthetic_snapshot'", Warning)

    assert [str(item.message) for item in captured] == [
        "Unknown table 'daily_report.synthetic_snapshot'"
    ]


@pytest.mark.asyncio
async def test_varchar_length_helper_only_expands_short_existing_columns():
    short = VarcharCursor("varchar(50)")
    await _ensure_varchar_length(short, "t_fullchain_archive", "电话号码", 500)
    assert any(
        "MODIFY COLUMN `电话号码` VARCHAR(500)" in sql
        for sql, _params in short.statements
    )

    current = VarcharCursor("varchar(500)")
    await _ensure_varchar_length(current, "t_fullchain_archive", "电话号码", 500)
    assert not any("ALTER TABLE" in sql for sql, _params in current.statements)

    missing = VarcharCursor(None)
    await _ensure_varchar_length(missing, "t_fullchain_archive", "电话号码", 500)
    assert not any("ALTER TABLE" in sql for sql, _params in missing.statements)
