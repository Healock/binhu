import os
import uuid

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-application-key")
os.environ.setdefault("TASK_EVENT_ENCRYPTION_KEY", "test-only-task-event-key-not-for-runtime")

import pytest

from services.task_commands import TaskCommandContractError
from services.task_view_state import (
    TASK_VIEW_STATE_INDEX_VERSION,
    apply_task_view_state_update,
    build_task_view_state_update,
    task_view_state_index_keys,
)


def build(*, domain="qmf", domain_revision=1, state=None, removed=False, **overrides):
    values = {
        "environment": "development",
        "run_id": "dev-state-index-test",
        "task_id": "t_fullchain:17",
        "domain": domain,
        "domain_revision": domain_revision,
        "task_revision": 9,
        "event_id": str(uuid.uuid4()),
        "observed_at": "2026-11-05T10:00:00Z",
        "state": {"state": "completed_match"} if state is None else state,
        "removed": removed,
    }
    values.update(overrides)
    return build_task_view_state_update(**values)


def test_index_keys_are_versioned_environment_run_scoped_and_do_not_reveal_values():
    assert TASK_VIEW_STATE_INDEX_VERSION == "v1"
    state = {"status": "confirmed", "candidate": {"name": "滨湖花园"}}
    dev = task_view_state_index_keys(
        environment="development", run_id="dev-state-index-test",
        domain="address_match", state=state,
    )
    other_run = task_view_state_index_keys(
        environment="development", run_id="dev-other-run",
        domain="address_match", state=state,
    )
    staging = task_view_state_index_keys(
        environment="staging", run_id="STG-20261105-02",
        domain="address_match", state=state,
    )

    assert len(dev) == 2
    assert all(":stateidx:v1:" in key for key in dev)
    assert set(dev).isdisjoint(other_run)
    assert set(dev).isdisjoint(staging)
    assert all("滨湖花园" not in key and "confirmed" not in key for key in dev)


@pytest.mark.parametrize(
    ("domain", "state", "expected_fields"),
    [
        ("address_match", {"status": "suggested", "candidate": {"community_name": "湖畔小区"}}, {"status", "small_community_name"}),
        ("watch", {"watch_marks": [{"category_id": 17, "name": "正文不入索引"}]}, {"category_id"}),
        ("qmf", {"state": "completed_mismatch", "result": "敏感业务正文"}, {"state"}),
        ("residence", {"state": "registered"}, {"state"}),
        ("registration", {"status": "awaiting_match", "manual_reason": "正文不入索引"}, {"status"}),
        ("review", {"state": "deep_pending", "feedback": "正文不入索引"}, {"state"}),
        ("task_graph", {"state": "blocked", "blocked": True}, {"state", "blocked"}),
    ],
)
def test_each_domain_indexes_only_allowlisted_facets(domain, state, expected_fields):
    keys = task_view_state_index_keys(
        environment="development", run_id="dev-state-index-test",
        domain=domain, state=state,
    )
    assert len(keys) == len(expected_fields)
    assert all("正文不入索引" not in key and "敏感业务正文" not in key for key in keys)


@pytest.mark.parametrize(
    ("domain", "state"),
    [
        ("qmf", {"state": "person-name"}),
        ("review", {"state": "free form"}),
        ("address_match", {"status": "address text"}),
        ("watch", {"watch_marks": [{"category_id": 0}]}),
        ("task_graph", {"state": "ready", "blocked": "yes"}),
    ],
)
def test_state_index_rejects_free_text_unknown_enums_and_invalid_ids(domain, state):
    with pytest.raises(TaskCommandContractError):
        task_view_state_index_keys(
            environment="development", run_id="dev-state-index-test",
            domain=domain, state=state,
        )


class _Redis:
    def __init__(self):
        self.record = {}
        self.memberships = set()
        self.indexes = {}

    async def eval(self, script, num_keys, record_key, *args):
        assert num_keys == 1
        self.script = script
        revision, event_id = int(args[0]), args[1]
        current = int(self.record.get("domain_revision", -1))
        if self.record.get("event_id") == event_id:
            return "duplicate"
        if revision < current:
            return "stale"
        if revision == current and current >= 0:
            return "revision_conflict"

        task_id = args[4]
        membership_key, count = args[14], int(args[15])
        new_keys = args[16:16 + count]
        for key in self.memberships:
            self.indexes[key].discard(task_id)
        self.memberships = set(new_keys)
        for key in new_keys:
            self.indexes.setdefault(key, set()).add(task_id)
        self.record = {
            "domain_revision": revision,
            "event_id": event_id,
            "record_key": record_key,
            "membership_key": membership_key,
            "index_keys": tuple(new_keys),
        }
        return "applied"


@pytest.mark.asyncio
async def test_apply_atomically_replaces_domain_memberships_and_fences_stale_events():
    redis = _Redis()
    old = build(
        domain="qmf", domain_revision=2,
        state={"state": "completed_match"},
    )
    current = build(
        domain="qmf", domain_revision=3,
        state={"state": "completed_mismatch"},
    )
    stale = build(
        domain="qmf", domain_revision=1,
        state={"state": "not_found"},
    )

    assert await apply_task_view_state_update(redis, old) == "applied"
    old_index = old["index_keys"][0]
    assert redis.indexes[old_index] == {old["task_id"]}
    assert await apply_task_view_state_update(redis, current) == "applied"
    new_index = current["index_keys"][0]
    assert redis.indexes[old_index] == set()
    assert redis.indexes[new_index] == {current["task_id"]}
    assert await apply_task_view_state_update(redis, stale) == "stale"
    assert redis.record["domain_revision"] == 3
    assert redis.record["index_keys"] == current["index_keys"]
    assert "SMEMBERS" in redis.script and "SREM" in redis.script and "SADD" in redis.script


@pytest.mark.asyncio
async def test_removed_state_clears_all_index_memberships():
    redis = _Redis()
    active = build(
        domain="watch", domain_revision=1,
        state={"watch_marks": [{"category_id": 3}, {"category_id": 4}]},
    )
    removed = build(
        domain="watch", domain_revision=2, state={}, removed=True,
    )

    assert await apply_task_view_state_update(redis, active) == "applied"
    assert len(redis.memberships) == 2
    assert await apply_task_view_state_update(redis, removed) == "applied"
    assert redis.memberships == set()
    assert all(redis.indexes[key] == set() for key in active["index_keys"])
    assert redis.record["index_keys"] == ()
