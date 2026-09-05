import os
from pathlib import Path

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from migrations.online_projection_queue_performance import build_parser


def test_migration_requires_explicit_apply_flag():
    parser = build_parser()
    assert parser.parse_args(["migrate"]).apply is False
    assert parser.parse_args(["migrate", "--apply"]).apply is True


def test_migration_contract_has_measure_verify_and_bounded_queue_index():
    source = Path(__file__).parents[1].joinpath(
        "migrations", "online_projection_queue_performance.py"
    ).read_text(encoding="utf-8")
    assert 'sub.add_parser("measure"' in source
    assert 'sub.add_parser("verify"' in source
    assert "(status,available_at,created_at,id)" in source
    assert "(source_kind,source_ref)" in source
    assert "EXPLAIN " in source
    assert "index_bytes" in source
