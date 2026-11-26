import os
import uuid
from unittest.mock import AsyncMock

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-application-key")
os.environ.setdefault("TASK_EVENT_ENCRYPTION_KEY", "test-only-task-event-key-not-for-runtime")

import pytest

from services import task_view_state
from services.task_commands import TaskCommandContractError
from services.task_view_state import (
    apply_task_view_state_update,
    build_task_view_state_update,
    decrypt_task_view_state,
    read_task_view_state,
    read_task_view_states_for_tasks,
    task_view_state_keys,
)


def build(**overrides):
    values = {
        "environment": "development",
        "run_id": "dev-task-view-test",
        "task_id": "t_fullchain:17",
        "domain": "qmf",
        "domain_revision": 4,
        "task_revision": 9,
        "event_id": str(uuid.uuid4()),
        "observed_at": "2026-11-05T10:00:00Z",
        "state": {"status": "completed_match", "error_code": ""},
    }
    values.update(overrides)
    return build_task_view_state_update(**values)


def test_task_view_state_key_is_scoped_by_environment_run_task_and_domain():
    qmf = task_view_state_keys("development", "dev-task-view-test", "t_fullchain:17", "qmf")
    review = task_view_state_keys("development", "dev-task-view-test", "t_fullchain:17", "review")
    staging = task_view_state_keys("staging", "STG-20261105-02", "t_fullchain:17", "qmf")
    task_graph = task_view_state_keys(
        "development", "dev-task-view-test", "t_fullchain:17", "task_graph",
    )

    assert qmf[0] != review[0]
    assert qmf[0] != staging[0]
    assert task_graph[0] != review[0]


def test_task_graph_state_uses_the_shared_encrypted_domain_contract():
    update = build(
        domain="task_graph",
        state={
            "state": "blocked", "blocked": True,
            "online_status": "blocked", "analysis_status": "ready",
            "dependency_state": "active",
        },
    )

    assert "blocked" not in repr(update)
    assert decrypt_task_view_state(update)["blocked"] is True


def test_task_view_state_is_encrypted_and_authenticates_all_revision_dimensions():
    update = build()

    assert "completed_match" not in repr(update)
    assert decrypt_task_view_state(update) == {"status": "completed_match", "error_code": ""}

    update["task_revision"] += 1
    with pytest.raises(TaskCommandContractError, match="authentication failed"):
        decrypt_task_view_state(update)


@pytest.mark.parametrize(
    "overrides",
    [
        {"domain": "arbitrary"},
        {"domain_revision": 0},
        {"task_revision": 0},
        {"removed": True},
        {"observed_at": "not-a-time"},
        {"task_id": "unregistered:17"},
    ],
)
def test_task_view_state_rejects_invalid_contract(overrides):
    with pytest.raises(TaskCommandContractError):
        build(**overrides)


class _Redis:
    def __init__(self):
        self.revision = -1
        self.event_id = ""
        self.values = {}

    async def eval(self, _script, _keys, _key, revision, event_id, *values):
        revision = int(revision)
        if self.event_id == event_id:
            return "duplicate"
        if revision < self.revision:
            return "stale"
        if revision == self.revision and self.revision >= 0:
            return "revision_conflict"
        self.revision = revision
        self.event_id = event_id
        self.values = values
        return "applied"


@pytest.mark.asyncio
async def test_task_view_state_revision_fence_is_independent_for_each_domain():
    redis = _Redis()
    current = build(domain_revision=8)
    delayed = build(domain_revision=7)

    assert await apply_task_view_state_update(redis, current) == "applied"
    assert await apply_task_view_state_update(redis, delayed) == "stale"
    assert await apply_task_view_state_update(redis, current) == "duplicate"


@pytest.mark.asyncio
async def test_task_view_state_read_checks_identity_and_decrypts_payload():
    update = build()
    envelope = update["envelope"]

    class Redis:
        async def hmget(self, key, *fields):
            assert key == update["keys"][0]
            values = {
                "environment": update["environment"], "run_id": update["run_id"],
                "task_id": update["task_id"], "domain": update["domain"],
                "domain_revision": str(update["domain_revision"]),
                "task_revision": str(update["task_revision"]), "event_id": update["event_id"],
                "observed_at": update["observed_at"], "removed": "0",
                "ciphertext": envelope["ciphertext"], "aad": envelope["aad"],
                "nonce": envelope["nonce"], "ciphertext_sha256": envelope["ciphertext_sha256"],
                "key_version": envelope["key_version"],
            }
            return [values[field] for field in fields]

    result = await read_task_view_state(
        Redis(), environment=update["environment"], run_id=update["run_id"],
        task_id=update["task_id"], domain=update["domain"], expected_task_revision=9,
    )
    assert result["state"] == {"status": "completed_match", "error_code": ""}
    assert result["domain_revision"] == 4

    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        await read_task_view_state(
            Redis(), environment=update["environment"], run_id=update["run_id"],
            task_id=update["task_id"], domain=update["domain"], expected_task_revision=10,
        )


@pytest.mark.asyncio
async def test_bulk_task_view_state_read_rejects_uninitialized_domains():
    class Pipeline:
        def __init__(self):
            self.count = 0

        def hmget(self, _key, *_fields):
            self.count += 1
            return self

        async def execute(self):
            return [[None] * 14 for _ in range(self.count)]

    class Redis:
        def pipeline(self, **_kwargs):
            return Pipeline()

    with pytest.raises(TaskCommandContractError, match="not initialized"):
        await read_task_view_states_for_tasks(
            Redis(), environment="development", run_id="dev-task-view-test",
            tasks=[{"task_id": "t_fullchain:17", "revision": 9}],
        )


@pytest.mark.asyncio
async def test_model_three_without_scan_still_emits_all_view_states(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "MYSQL_ONLINE_DATA_DB", "Staging_OnlineData")
    monkeypatch.setattr(settings, "MYSQL_REGISTRY_DB", "Staging_RegistryData")
    monkeypatch.setattr(task_view_state, "task_view_state_events_enabled", lambda: True)
    enqueue = AsyncMock()
    monkeypatch.setattr(task_view_state, "enqueue_source_task_view_state", enqueue)
    monkeypatch.setattr(task_view_state, "enqueue_online_task_graph_view_state", AsyncMock())
    cursor = AsyncMock()
    cursor.fetchone.side_effect = [
        ("疑似未注销模型三", 17, 1, "hash", None, "synthetic"),
        None,  # address match
        None,  # QMF scan
        None,  # Residence status
        None,  # Registration link
    ]
    cursor.fetchall.return_value = []

    await task_view_state.enqueue_current_task_view_state_snapshot(
        cursor, source_id=1, snapshot={"核查结果": ""},
    )

    states = {call.kwargs["domain"]: call.kwargs["state"] for call in enqueue.await_args_list}
    assert set(states) == set(task_view_state.TASK_VIEW_STATE_DOMAINS)
    assert states["qmf"] == {"state": "not_scanned", "status": "not_scanned"}


@pytest.mark.asyncio
async def test_same_view_state_is_emitted_once_for_each_run(monkeypatch):
    from config import settings
    from services import domain_events

    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "staging")
    monkeypatch.setattr(settings, "MYSQL_ONLINE_DATA_DB", "Staging_OnlineData")
    monkeypatch.setattr(settings, "TASK_EVENT_ENCRYPTION_KEY", "synthetic-event-key-for-tests-123456")
    enqueue = AsyncMock(return_value="event")
    monkeypatch.setattr(domain_events, "enqueue_task_view_state_event", enqueue)
    cursor = AsyncMock()
    source = ("疑似未注销模型三", 17, 1, "hash", None, "synthetic")
    previous = (0, "")
    state = {"status": "not_scanned"}

    for run_id in ("STG-20261126-03", "STG-20261126-04"):
        monkeypatch.setattr(settings, "TASK_READ_MODEL_RUN_ID", run_id)
        cursor.fetchone.side_effect = [source, previous]
        assert await task_view_state.enqueue_source_task_view_state(
            cursor, source_id=1, domain="qmf", state=state,
        ) == "event"
        revision, _, fingerprint, *_ = cursor.execute.await_args.args[1]
        previous = (revision, fingerprint)
        cursor.fetchone.side_effect = [source, previous]
        assert await task_view_state.enqueue_source_task_view_state(
            cursor, source_id=1, domain="qmf", state=state,
        ) is None

    assert [c.kwargs["domain_revision"] for c in enqueue.await_args_list] == [1, 2]
