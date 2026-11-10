import os
import uuid
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-application-key")
os.environ.setdefault("TASK_EVENT_ENCRYPTION_KEY", "test-only-task-event-key-not-for-any-runtime-use")

from services.task_commands import (
    TaskCommandContractError,
    TaskAuthorizationResult,
    decrypt_task_snapshot,
    encrypt_task_snapshot,
    normalize_action,
    TaskCommandSpec,
    TaskMutationResult,
    execute_task_command,
    task_row_group_token,
    validate_task_command_transition,
)


def test_encrypted_snapshot_is_bound_to_its_event_identity():
    event_id = str(uuid.uuid4())
    task_id = "t_fixture:17"
    snapshot = {"备注": "合成敏感正文", "核查结果": "待核查"}
    envelope = encrypt_task_snapshot(
        snapshot,
        environment="development",
        task_id=task_id,
        revision=4,
        event_id=event_id,
    )

    assert decrypt_task_snapshot(
        envelope,
        environment="development",
        task_id=task_id,
        revision=4,
        event_id=event_id,
    ) == snapshot
    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        decrypt_task_snapshot(
            envelope,
            environment="development",
            task_id=task_id,
            revision=5,
            event_id=event_id,
        )


def test_v2_snapshot_is_bound_to_command_identity():
    event_id = str(uuid.uuid4())
    command_id = str(uuid.uuid4())
    task_id = "t_fixture:18"
    envelope = encrypt_task_snapshot(
        {"核查结果": "待核查"}, environment="development", task_id=task_id,
        revision=1, event_id=event_id, command_id=command_id,
    )
    assert decrypt_task_snapshot(
        envelope, environment="development", task_id=task_id, revision=1,
        event_id=event_id, command_id=command_id,
    ) == {"核查结果": "待核查"}
    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        decrypt_task_snapshot(
            envelope, environment="development", task_id=task_id, revision=1,
            event_id=event_id, command_id=str(uuid.uuid4()),
        )


def test_row_group_token_is_environment_scoped_and_authenticated_in_aad():
    snapshot = {"姓名": "合成对象", "身份证号": "synthetic-person-id"}
    token = task_row_group_token(snapshot, parser_type="全链条", environment="development")
    assert len(token) == 64
    assert token == token.lower()
    assert task_row_group_token(
        snapshot, parser_type="全链条", environment="development",
    ) == token
    assert task_row_group_token(
        snapshot, parser_type="全链条", environment="staging",
    ) != token

    event_id = str(uuid.uuid4())
    command_id = str(uuid.uuid4())
    envelope = encrypt_task_snapshot(
        snapshot, environment="development", task_id="t_fullchain:17",
        revision=1, event_id=event_id, command_id=command_id,
        row_group_token=token,
    )
    assert envelope["key_version"] == "task-event-v3"
    assert decrypt_task_snapshot(
        envelope, environment="development", task_id="t_fullchain:17",
        revision=1, event_id=event_id, command_id=command_id,
    ) == snapshot
    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        decrypt_task_snapshot(
            envelope, environment="development", task_id="t_fullchain:17",
            revision=1, event_id=event_id, command_id=command_id,
            row_group_token="0" * 64,
        )


def test_command_action_aliases_are_explicit_and_unknown_actions_fail_closed():
    assert normalize_action("bulk_assign_local") == "assign"
    assert normalize_action("unverifiable_review_decision") == "review_deep"
    with pytest.raises(TaskCommandContractError, match="unregistered"):
        normalize_action("new_action_without_contract")


def test_task_command_checks_revision_before_authorizing_and_mutating():
    class Cursor:
        def __init__(self):
            self.fetch_count = 0

        async def execute(self, query, params):
            assert params == (7,)
            if self.fetch_count == 0:
                assert "FOR UPDATE" in query
            else:
                assert "FROM _online_source_rows WHERE id=%s" in query

        async def fetchone(self):
            self.fetch_count += 1
            if self.fetch_count == 1:
                return ("t_suspect", 17, 4, None, '{"社区":"滨湖社区"}')
            return ("t_suspect", 17, 5, None)

    sequence = []

    async def authorize(_cur, source):
        sequence.append(("authorize", source["revision"]))
        return TaskAuthorizationResult("fixture_edit", ("滨湖社区",))

    async def mutate(_cur, source):
        sequence.append(("mutate", source["revision"]))
        return TaskMutationResult(
            revision=5,
            snapshot={"社区": "滨湖社区"},
            changed_fields=("community",),
        )

    spec = TaskCommandSpec(
        actor_id=9,
        environment="development",
        parser_type="t_suspect",
        task_id="t_default:17",
        source_id=7,
        action="edit",
        event_type="online.task.changed",
        expected_revision=4,
        audiences=("authenticated",),
    )
    with patch("services.task_commands._append_command_and_outbox", new=AsyncMock()) as enqueue:
        outcome = asyncio.run(execute_task_command(
            Cursor(), spec=spec, authorize=authorize, mutate=mutate,
        ))

    assert outcome.revision == 5
    assert sequence == [("authorize", 4), ("mutate", 4)]
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["authorization"] == TaskAuthorizationResult(
        "fixture_edit", ("滨湖社区",),
    )


def test_task_command_revision_conflict_never_runs_mutation():
    class Cursor:
        async def execute(self, _query, _params):
            pass

        async def fetchone(self):
            return ("t_suspect", 17, 5, None, {})

    mutate = AsyncMock()
    spec = TaskCommandSpec(
        actor_id=9,
        environment="development",
        parser_type="t_suspect",
        task_id="t_default:17",
        source_id=7,
        action="edit",
        event_type="online.task.changed",
        expected_revision=4,
        audiences=("authenticated",),
    )
    with pytest.raises(TaskCommandContractError, match="revision conflict"):
        asyncio.run(execute_task_command(
            Cursor(), spec=spec, authorize=AsyncMock(), mutate=mutate,
        ))
    mutate.assert_not_awaited()


def test_task_command_requires_explicit_authorization_result():
    class Cursor:
        def __init__(self):
            self.fetch_count = 0

        async def execute(self, _query, _params):
            pass

        async def fetchone(self):
            self.fetch_count += 1
            return ("t_suspect", 17, 4, None, {})

    async def mutate(_cur, _source):
        return TaskMutationResult(5, {"社区": "滨湖社区"}, ("community",))

    spec = TaskCommandSpec(
        actor_id=9,
        environment="development",
        parser_type="t_suspect",
        task_id="t_default:17",
        source_id=7,
        action="edit",
        event_type="online.task.changed",
        expected_revision=4,
        audiences=("authenticated",),
    )
    with pytest.raises(TaskCommandContractError, match="authorization result"):
        asyncio.run(execute_task_command(
            Cursor(), spec=spec, authorize=AsyncMock(return_value=None), mutate=mutate,
        ))


def test_task_command_refuses_to_append_when_mutation_did_not_advance_source():
    class Cursor:
        def __init__(self):
            self.fetch_count = 0

        async def execute(self, _query, _params):
            pass

        async def fetchone(self):
            self.fetch_count += 1
            return ("t_suspect", 17, 4, None, {}) if self.fetch_count == 1 else (
                "t_suspect", 17, 4, None,
            )

    spec = TaskCommandSpec(
        actor_id=9,
        environment="development",
        parser_type="t_suspect",
        task_id="t_default:17",
        source_id=7,
        action="edit",
        event_type="online.task.changed",
        expected_revision=4,
        audiences=("authenticated",),
    )
    async def mutate(_cur, _source):
        return TaskMutationResult(5, {"社区": "滨湖社区"}, ("community",))

    with patch("services.task_commands._append_command_and_outbox", new=AsyncMock()) as enqueue:
        with pytest.raises(TaskCommandContractError, match="revision was not persisted"):
            asyncio.run(execute_task_command(
                Cursor(), spec=spec,
                authorize=AsyncMock(return_value=TaskAuthorizationResult("fixture")),
                mutate=mutate,
            ))
    enqueue.assert_not_awaited()


@pytest.mark.parametrize(
    ("action", "event_type", "expected", "revision"),
    [
        ("create", "online.task.created", None, 1),
        ("edit", "online.task.changed", 4, 5),
        ("archive", "online.task.archived", 4, 5),
        ("delete", "online.task.deleted", 4, 5),
    ],
)
def test_task_command_transition_contract(action, event_type, expected, revision):
    validate_task_command_transition(
        action=action, event_type=event_type,
        expected_revision=expected, revision=revision,
    )
