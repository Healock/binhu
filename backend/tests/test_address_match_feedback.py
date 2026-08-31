import pytest

from services.address_match_feedback import (
    ACTIVE,
    CONFLICT,
    apply_feedback_memory,
    feedback_hmac,
    feedback_transition,
    record_feedback_confirmation,
)


ENTRY = {
    "id": 7,
    "name": "示例花园",
    "community_id": 3,
    "community_name": "示例社区",
    "enabled": True,
}


@pytest.fixture(autouse=True)
def configure_security_key(monkeypatch):
    monkeypatch.setenv("MYSQL_PASSWORD", "test-password")
    monkeypatch.setenv("ENCRYPTION_KEY", "test-encryption-key")
    monkeypatch.setenv("REGISTRY_HMAC_KEY", "test-registry-hmac-key")


def test_feedback_hmac_is_stable_and_does_not_expose_plain_address():
    first = feedback_hmac(" 示例花园（1栋）101室 ", "示例社区")
    second = feedback_hmac("示例花园1幢101室", "示例社区")
    assert first == second
    assert len(first) == 64
    assert "示例" not in first
    assert feedback_hmac("", "示例社区") == ""


def test_consistent_feedback_stays_active_and_conflicting_feedback_is_fused():
    active = feedback_transition(
        None,
        confirmed_entry_id=7,
        community_id=3,
    )
    assert active.status == ACTIVE
    repeated = feedback_transition(
        {
            "status": active.status,
            "confirmed_entry_id": active.confirmed_entry_id,
            "community_id": 3,
            "confirmation_count": active.confirmation_count,
            "conflict_count": active.conflict_count,
        },
        confirmed_entry_id=7,
        community_id=3,
    )
    assert repeated.status == ACTIVE
    assert repeated.confirmation_count == 2

    conflict = feedback_transition(
        {
            "status": repeated.status,
            "confirmed_entry_id": repeated.confirmed_entry_id,
            "community_id": 3,
            "confirmation_count": repeated.confirmation_count,
            "conflict_count": repeated.conflict_count,
        },
        confirmed_entry_id=8,
        community_id=3,
    )
    assert conflict.status == CONFLICT
    assert conflict.confirmed_entry_id is None
    assert conflict.conflict_count == 1


def test_exact_feedback_memory_becomes_automatic_but_never_overrides_conflict():
    address = "示例花园1幢101室"
    key = feedback_hmac(address, "示例社区")
    memories = {
        key: {
            "status": ACTIVE,
            "community_id": 3,
            "confirmed_entry_id": 7,
        }
    }
    result = apply_feedback_memory(
        {
            "status": "ambiguous",
            "score": 0.2,
            "method": "规则",
            "reason": "信息不足",
            "candidate": None,
            "candidates": [],
            "version": "rule-v2",
        },
        address=address,
        community_name="示例社区",
        memories=memories,
        entries_by_id={7: ENTRY},
    )
    assert result["status"] == "suggested"
    assert result["candidate"]["entry_id"] == 7
    assert result["method"] == "人工反馈记忆"

    hard_conflict = apply_feedback_memory(
        {"status": "conflict", "candidate": None},
        address=address,
        community_name="示例社区",
        memories=memories,
        entries_by_id={7: ENTRY},
    )
    assert hard_conflict["status"] == "conflict"


def test_conflicted_or_cross_community_memory_is_not_reused():
    address = "示例花园1幢101室"
    key = feedback_hmac(address, "示例社区")
    base = {"status": "ambiguous", "candidate": None, "candidates": []}
    conflicted = apply_feedback_memory(
        base,
        address=address,
        community_name="示例社区",
        memories={key: {"status": CONFLICT, "community_id": 3, "confirmed_entry_id": None}},
        entries_by_id={7: ENTRY},
    )
    assert conflicted is base

    cross_community = apply_feedback_memory(
        base,
        address=address,
        community_name="示例社区",
        memories={key: {"status": ACTIVE, "community_id": 4, "confirmed_entry_id": 7}},
        entries_by_id={7: ENTRY},
    )
    assert cross_community is base


@pytest.mark.asyncio
async def test_feedback_persistence_uses_digest_without_copying_address_text():
    class Cursor:
        def __init__(self):
            self.calls = []

        async def execute(self, sql, params=None):
            self.calls.append((str(sql), params))

        async def fetchone(self):
            return None

    cursor = Cursor()
    raw_address = "示例花园1幢101室"
    status = await record_feedback_confirmation(
        cursor,
        parser_type="全链条",
        row_key="a" * 32,
        address=raw_address,
        community_name="示例社区",
        community_id=3,
        confirmed_entry_id=7,
        confirmed_by=9,
    )
    assert status == ACTIVE
    flattened = repr(cursor.calls)
    assert raw_address not in flattened
    assert "address_hmac" in flattened
