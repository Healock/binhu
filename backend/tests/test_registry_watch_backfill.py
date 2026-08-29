import os
from pathlib import Path

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from migrations.registry_watch_people import build_parser
from services.registry_watch_backfill import empty_backfill_summary, valid_identity_hmac


def test_identity_hmac_validation_rejects_non_digest_values():
    assert valid_identity_hmac("a" * 64)
    assert valid_identity_hmac("A" * 64)
    assert not valid_identity_hmac("")
    assert not valid_identity_hmac("1" * 63)
    assert not valid_identity_hmac("x" * 64)


def test_backfill_summary_is_safe_aggregate_only():
    summary = empty_backfill_summary()
    assert summary["eligible"] == 0
    assert "name" not in summary
    assert "identity_number" not in summary
    assert "phone" not in summary


def test_migration_requires_explicit_apply_flag():
    parser = build_parser()
    assert parser.parse_args(["migrate"]).apply is False
    assert parser.parse_args(["migrate", "--apply", "--batch-size", "25"]).apply is True


def test_merge_history_query_uses_non_reserved_alias():
    source = (
        Path(__file__).resolve().parents[1] / "routers" / "registry_extended.py"
    ).read_text(encoding="utf-8")
    assert "registry_merge_history undo_record" in source
    assert "registry_merge_history undo WHERE" not in source
