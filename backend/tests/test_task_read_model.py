import os
import re
import uuid

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-application-key")
os.environ.setdefault("TASK_EVENT_ENCRYPTION_KEY", "test-only-task-event-key-not-for-runtime")

import pytest

from services.task_commands import (
    TaskCommandContractError, encrypt_task_snapshot, task_row_group_token,
)
from services.task_read_model import (
    TASK_READ_MODEL_SCAN_BATCH_MAX,
    apply_read_model_update,
    build_read_model_update,
    close_task_read_model_scan,
    count_task_read_model_groups,
    create_task_read_model_scan,
    index_key,
    query_task_ids,
    query_task_records,
    query_task_read_model_group_ids,
    read_task_read_model_group_records,
    read_task_read_model_scan,
    read_task_read_model_scan_records,
    read_task_snapshot,
    search_token,
    task_read_model_keys,
)
from migrations.task_read_model_backfill import event_ids
from routers import mobile_tasks
from fastapi import HTTPException


SECRET = "test-only-task-event-key-not-for-runtime"
TASK_ID = "t_fullchain:17"
EVENT_ID = str(uuid.uuid4())
COMMAND_ID = str(uuid.uuid4())
RUN_ID = "dev-test-1"
ROW_GROUP_TOKEN = "a" * 64


def event(revision=1, *, task_id=TASK_ID, environment="development", event_id=EVENT_ID):
    snapshot = {"姓名": "合成姓名", "地址": "合成地址 1 号", "核查结果": "待核查", "社区": "合成社区"}
    return {
        "schema_version": 6,
        "command_id": COMMAND_ID,
        "event_id": event_id,
        "event_type": "task.saved",
        "task_id": task_id,
        "source_id": 17,
        "revision": revision,
        "environment": environment,
        "run_id": RUN_ID,
        "timestamp": "2026-11-04T00:00:00Z",
        "row_group_token": ROW_GROUP_TOKEN,
        "encrypted_payload": encrypt_task_snapshot(
            snapshot, environment=environment, task_id=task_id,
            revision=revision, event_id=event_id, command_id=COMMAND_ID,
            row_group_token=ROW_GROUP_TOKEN,
        ),
    }


def test_read_model_update_contains_only_ciphertext_and_environment_scoped_indexes():
    update = build_read_model_update(event(), secret=SECRET)
    rendered = repr(update)
    assert "合成姓名" not in rendered
    assert "合成地址" not in rendered
    assert update["index_keys"]
    assert update["row_group_token"] == ROW_GROUP_TOKEN
    assert re.fullmatch(r"[0-9a-f]{64}", update["community_token"])
    assert all("合成" not in key for key in update["index_keys"])
    assert update["keys"][0].startswith("binhu:task-rm:v4:development:dev-test-1:task:")
    assert search_token("合成姓名", field="name", environment="development", secret=SECRET) != search_token(
        "合成姓名", field="name", environment="staging", secret=SECRET,
    )


def test_archived_task_event_removes_task_from_active_redis_read_model():
    archived = event()
    archived["event_type"] = "task.archived"

    update = build_read_model_update(archived, secret=SECRET)

    assert update["tombstone"] is True
    assert update["index_keys"] == []


def test_legacy_task_event_derives_group_token_after_decrypting_v2_snapshot():
    value = event()
    value["schema_version"] = 4
    value.pop("row_group_token")
    value["encrypted_payload"] = encrypt_task_snapshot(
        {"姓名": "合成姓名", "地址": "合成地址 1 号", "核查结果": "待核查", "社区": "合成社区"},
        environment="development", task_id=TASK_ID, revision=1,
        event_id=EVENT_ID, command_id=COMMAND_ID,
    )
    update = build_read_model_update(value, secret=SECRET)
    assert update["row_group_token"] == task_row_group_token(
        {"姓名": "合成姓名", "地址": "合成地址 1 号", "核查结果": "待核查", "社区": "合成社区"},
        parser_type="全链条", environment="development",
    )


def test_model_three_community_token_uses_registered_table_parser():
    model_task_id = "t_suspect_unrevoked:17"
    model_event_id = str(uuid.uuid4())
    snapshot = {"姓名": "合成姓名", "联系方式": "合成电话", "下发社区": "模型三社区"}
    value = {
        **event(task_id=model_task_id, event_id=model_event_id),
        "source_id": 17,
        "encrypted_payload": encrypt_task_snapshot(
            snapshot, environment="development", task_id=model_task_id,
            revision=1, event_id=model_event_id, command_id=COMMAND_ID,
            row_group_token=ROW_GROUP_TOKEN,
        ),
    }
    update = build_read_model_update(value, secret=SECRET)
    assert update["community_token"] == search_token(
        "模型三社区", field="group-community", environment="development", secret=SECRET,
    )


def test_read_model_key_does_not_expose_task_identity():
    task_key, indexes_key, prefix = task_read_model_keys("staging", "STG-20261104-01", TASK_ID)
    assert task_key.endswith(":task:" + TASK_ID)
    assert indexes_key.endswith(":indexes:" + TASK_ID)
    assert prefix == "binhu:task-rm:v4:staging:STG-20261104-01"


@pytest.mark.parametrize(("environment", "run_id", "task_id"), [
    ("development", "STG-20261104-01", TASK_ID),
    ("staging", "STG-20261104-01", "t_unknown:17"),
    ("staging", "STG-20261104-01", "t_fullchain:0"),
    ("development", "dev-test-1", "t_fullchain:17:extra"),
])
def test_read_model_key_rejects_mismatched_or_unregistered_identity(environment, run_id, task_id):
    with pytest.raises(TaskCommandContractError, match="invalid task read-model identity"):
        task_read_model_keys(environment, run_id, task_id)


def test_backfill_event_identity_is_stable_for_retry_and_changes_with_revision():
    first = event_ids("development", RUN_ID, "fullchain", 17, 3)
    assert first == event_ids("development", RUN_ID, "fullchain", 17, 3)
    assert first != event_ids("development", RUN_ID, "fullchain", 17, 4)


def test_unmigrated_mobile_read_paths_fail_closed_when_read_model_is_enabled(monkeypatch):
    monkeypatch.setattr(mobile_tasks.settings, "TASK_READ_MODEL_ENABLED", True)
    with pytest.raises(HTTPException) as error:
        mobile_tasks._require_legacy_read_model_disabled()
    assert error.value.status_code == 503
    assert error.value.detail == {"error": "projection_pending"}


def test_read_model_requires_bound_encrypted_snapshot_and_registered_identity():
    value = event()
    value["revision"] = 2
    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        build_read_model_update(value, secret=SECRET)
    with pytest.raises(TaskCommandContractError, match="invalid task read-model identity"):
        task_read_model_keys("production", RUN_ID, "")


def test_delete_event_becomes_tombstone_without_payload():
    value = event()
    value.update(event_type="task.deleted", revision=2, event_id=str(uuid.uuid4()))
    update = build_read_model_update(value, secret=SECRET)
    assert update["tombstone"] is True
    assert update["envelope"] == {}
    assert update["index_keys"] == []


class RedisStub:
    def __init__(self):
        self.call = None

    async def eval(self, *args):
        self.call = args
        return "applied"


@pytest.mark.asyncio
async def test_redis_update_is_one_atomic_script_and_does_not_pass_index_values():
    update = build_read_model_update(event(), secret=SECRET)
    redis = RedisStub()
    assert await apply_read_model_update(redis, update) == "applied"
    assert redis.call[1] == 3
    assert redis.call[-1].endswith("|")
    assert ROW_GROUP_TOKEN in redis.call
    assert "合成姓名" not in repr(redis.call)
    assert "合成地址" not in repr(redis.call)


class RedisReadStub:
    def __init__(self, row):
        self.row = row

    async def hgetall(self, key):
        return self.row


@pytest.mark.asyncio
async def test_readback_requires_exact_revision_and_event_identity():
    value = event()
    envelope = value["encrypted_payload"]
    row = {**envelope, "revision": "1", "event_id": EVENT_ID,
           "environment": "development", "run_id": RUN_ID, "command_id": COMMAND_ID,
           "task_id": TASK_ID, "source_id": "17", "tombstone": "0",
           "row_group_token": ROW_GROUP_TOKEN}
    redis = RedisReadStub(row)
    assert await read_task_snapshot(
        redis, task_read_model_keys("development", RUN_ID, TASK_ID)[0],
        environment="development", run_id=RUN_ID, task_id=TASK_ID,
        event_id=EVENT_ID, command_id=COMMAND_ID, revision=1,
    ) == {"姓名": "合成姓名", "地址": "合成地址 1 号", "核查结果": "待核查", "社区": "合成社区"}
    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        await read_task_snapshot(
            redis, task_read_model_keys("development", RUN_ID, TASK_ID)[0],
            environment="development", run_id=RUN_ID, task_id=TASK_ID,
            event_id=EVENT_ID, command_id=COMMAND_ID, revision=2,
        )
    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        await read_task_snapshot(
            redis, task_read_model_keys("development", RUN_ID, TASK_ID)[0],
            environment="development", run_id=RUN_ID, task_id=TASK_ID,
            event_id=EVENT_ID, command_id=str(uuid.uuid4()), revision=1,
        )
    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        await read_task_snapshot(
            redis, "some:caller:chosen:key", environment="development", run_id=RUN_ID,
            task_id=TASK_ID, event_id=EVENT_ID, command_id=COMMAND_ID, revision=1,
        )


@pytest.mark.asyncio
async def test_readback_checks_stored_environment_task_and_source_identity():
    value = event()
    row = {
        **value["encrypted_payload"], "revision": "1", "event_id": EVENT_ID,
        "environment": "staging", "run_id": RUN_ID, "command_id": COMMAND_ID,
        "task_id": TASK_ID, "source_id": "17", "tombstone": "0",
        "row_group_token": ROW_GROUP_TOKEN,
    }
    redis = RedisReadStub(row)
    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        await read_task_snapshot(
            redis, task_read_model_keys("development", RUN_ID, TASK_ID)[0],
            environment="development", run_id=RUN_ID, task_id=TASK_ID,
            event_id=EVENT_ID, command_id=COMMAND_ID, revision=1, source_id=17,
        )
    row.update(environment="development", task_id="t_fullchain:18")
    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        await read_task_snapshot(
            redis, task_read_model_keys("development", RUN_ID, TASK_ID)[0],
            environment="development", run_id=RUN_ID, task_id=TASK_ID,
            event_id=EVENT_ID, command_id=COMMAND_ID, revision=1, source_id=17,
        )
    row.update(task_id=TASK_ID, source_id="18")
    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        await read_task_snapshot(
            redis, task_read_model_keys("development", RUN_ID, TASK_ID)[0],
            environment="development", run_id=RUN_ID, task_id=TASK_ID,
            event_id=EVENT_ID, command_id=COMMAND_ID, revision=1, source_id=17,
        )


class RedisQueryStub:
    async def execute_command(self, *args):
        self.args = args
        self.calls = getattr(self, "calls", []) + [args]
        if args[0] in {"ZUNIONSTORE", "ZINTERSTORE"}:
            key = args[1]
            self.zsets = getattr(self, "zsets", {})
            self.zsets[key] = [("t_fullchain:17", 4.0), ("t_fullchain:18", 2.0)]
            return len(self.zsets[key])
        raise AssertionError(f"unexpected Redis command: {args[0]}")

    async def zcard(self, key):
        self.calls.append(("ZCARD", key))
        return len(self.zsets[key])

    async def zrange(self, key, start, end, *, withscores):
        assert withscores is True
        self.calls.append(("ZRANGE", key, start, end, "WITHSCORES"))
        return sorted(self.zsets[key], key=lambda item: (item[1], item[0]))[start:end + 1]

    async def zrevrange(self, key, start, end, *, withscores):
        assert withscores is True
        self.calls.append(("ZREVRANGE", key, start, end, "WITHSCORES"))
        return sorted(self.zsets[key], key=lambda item: (item[1], item[0]), reverse=True)[start:end + 1]

    async def zunionstore(self, *_args):
        return 1

    async def expire(self, *_args):
        self.expirations = getattr(self, "expirations", []) + [_args]
        return True

    async def delete(self, *_args):
        self.deleted = _args
        return 1


class RedisPipelineStub:
    def __init__(self, client):
        self.client = client
        self.keys = []

    def hgetall(self, key):
        self.keys.append(key)
        return self

    async def execute(self):
        self.client.pipeline_keys = list(self.keys)
        return [self.client.rows[key] for key in self.keys]


class RedisRecordQueryStub(RedisQueryStub):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.pipeline_keys = []
        self.pipeline_count = 0

    async def execute_command(self, *_args):
        args = _args
        self.args = args
        self.calls = getattr(self, "calls", []) + [args]
        if args[0] in {"ZUNIONSTORE", "ZINTERSTORE"}:
            key = args[1]
            ids = [key.rsplit(":task:", 1)[1] for key in self.rows]
            self.zsets = getattr(self, "zsets", {})
            self.zsets[key] = [(task_id, 1.0) for task_id in ids]
            return len(ids)
        raise AssertionError(f"unexpected Redis command: {args[0]}")

    def pipeline(self, *, transaction):
        assert transaction is False
        self.pipeline_count += 1
        return RedisPipelineStub(self)


@pytest.mark.asyncio
@pytest.mark.parametrize(("descending", "offset", "limit", "expected", "range_command", "bounds"), [
    (True, 0, 10, ["t_fullchain:17", "t_fullchain:18"], "ZREVRANGE", (0, 9)),
    (False, 1, 1, ["t_fullchain:17"], "ZRANGE", (1, 1)),
])
async def test_query_uses_hmac_indexes_and_bounded_stable_page(
    descending, offset, limit, expected, range_command, bounds,
):
    redis = RedisQueryStub()
    ids = await query_task_ids(
        redis, environment="development", run_id=RUN_ID, filters={"community": "合成社区"},
        secret=SECRET, offset=offset, limit=limit, descending=descending,
    )
    assert ids == expected
    assert "合成社区" not in repr(redis.args)
    assert redis.calls[0][0] == "ZINTERSTORE"
    assert redis.calls[0][-2:] == ("AGGREGATE", "MIN")
    assert next(call for call in redis.calls if call[0] == range_command)[2:4] == bounds
    assert len(redis.deleted) == 1
    assert redis.expirations == [(redis.deleted[0], 30)]


@pytest.mark.asyncio
async def test_query_uses_union_for_scope_values_and_intersection_for_search_grams():
    redis = RedisQueryStub()
    await query_task_ids(
        redis, environment="development", run_id=RUN_ID,
        filters={"task_type": "t_fullchain", "community": ["社区甲", "社区乙"]},
        search="滨湖 1", secret=SECRET, offset=0, limit=10,
    )
    assert any(call[0] == "ZUNIONSTORE" and call[-2:] == ("AGGREGATE", "MIN")
               for call in redis.calls)
    intersection = next(call for call in redis.calls if call[0] == "ZINTERSTORE")
    assert intersection[2] == 5
    assert intersection[-2:] == ("AGGREGATE", "MIN")
    assert len(redis.expirations) == 2
    assert all(ttl == 30 for _key, ttl in redis.expirations)


@pytest.mark.asyncio
async def test_query_records_batches_hash_reads_and_validates_identity():
    second_event_id = str(uuid.uuid4())
    second_command_id = str(uuid.uuid4())
    records = {}
    for task_id, source_id, revision, event_id, command_id, name in (
        (TASK_ID, 17, 4, EVENT_ID, COMMAND_ID, "Alice"),
        ("t_fullchain:18", 18, 2, second_event_id, second_command_id, "Bob"),
    ):
        encrypted = encrypt_task_snapshot(
                {"姓名": name, "社区": "合成社区", "地址": "合成路 1 号"},
            environment="development", task_id=task_id, revision=revision,
            event_id=event_id, command_id=command_id,
        )
        key = task_read_model_keys("development", RUN_ID, task_id)[0]
        records[key] = {
            **encrypted, "environment": "development", "run_id": RUN_ID,
            "task_id": task_id, "source_id": str(source_id), "revision": str(revision),
            "event_id": event_id, "command_id": command_id, "updated_at": "2026-11-04",
            "tombstone": "0",
        }
    redis = RedisRecordQueryStub(records)
    rows, total = await query_task_records(
        redis, environment="development", run_id=RUN_ID, parser_type="t_fullchain",
        communities=["合成社区"], keyword=None, filters={}, secret=SECRET,
        sort_by="姓名", sort_order="asc", offset=0, limit=10,
    )
    assert [row["task_id"] for row in rows] == [TASK_ID, "t_fullchain:18"]
    assert total == 2
    assert redis.pipeline_count == 1
    assert len(redis.pipeline_keys) == 2
    assert rows[0]["source_id"] == 17


@pytest.mark.asyncio
async def test_query_rejects_unregistered_fields_and_bounds_filters():
    redis = RedisQueryStub()
    with pytest.raises(TaskCommandContractError, match="invalid task read-model filters"):
        await query_task_ids(
            redis, environment="development", run_id=RUN_ID,
            filters={"arbitrary:redis:key": "x"}, secret=SECRET,
        )
    with pytest.raises(TaskCommandContractError, match="invalid task read-model filters"):
        await query_task_ids(
            redis, environment="development", run_id=RUN_ID,
            filters={"社区": ["社区"] * 51}, secret=SECRET,
        )


@pytest.mark.asyncio
async def test_query_records_rejects_a_hash_from_another_task():
    second_event_id = str(uuid.uuid4())
    encrypted = encrypt_task_snapshot(
        {"姓名": "甲", "社区": "合成社区"}, environment="development",
        task_id="t_fullchain:18", revision=1, event_id=second_event_id,
        command_id=COMMAND_ID,
    )
    key = task_read_model_keys("development", RUN_ID, TASK_ID)[0]
    redis = RedisRecordQueryStub({key: {
        **encrypted, "environment": "development", "run_id": RUN_ID,
        "task_id": "t_fullchain:18", "source_id": "17", "revision": "1",
        "event_id": second_event_id, "command_id": COMMAND_ID, "tombstone": "0",
    }})
    with pytest.raises(TaskCommandContractError, match="task read-model identity mismatch"):
        await query_task_records(
            redis, environment="development", run_id=RUN_ID, parser_type="t_fullchain",
            communities=["合成社区"], keyword=None, filters={}, secret=SECRET,
            sort_by=None, sort_order="desc", offset=0, limit=10,
        )


@pytest.mark.asyncio
async def test_scan_record_decoder_batches_identity_validation_and_decryption():
    encrypted = encrypt_task_snapshot(
        {"姓名": "合成姓名", "社区": "合成社区"}, environment="development",
        task_id=TASK_ID, revision=4, event_id=EVENT_ID, command_id=COMMAND_ID,
    )
    key = task_read_model_keys("development", RUN_ID, TASK_ID)[0]
    redis = RedisRecordQueryStub({key: {
        **encrypted,
        "environment": "development",
        "run_id": RUN_ID,
        "task_id": TASK_ID,
        "source_id": "17",
        "revision": "4",
        "event_id": EVENT_ID,
        "command_id": COMMAND_ID,
        "updated_at": "2026-11-04T00:00:00Z",
        "tombstone": "0",
    }})
    rows = await read_task_read_model_scan_records(
        redis, environment="development", run_id=RUN_ID, parser_type="t_fullchain",
        records=[{"task_id": TASK_ID, "indexed_revision": 4}],
    )
    assert rows[0]["source_id"] == 17
    assert rows[0]["revision"] == 4
    assert rows[0]["values"] == {"姓名": "合成姓名", "社区": "合成社区"}
    assert redis.pipeline_count == 1

    with pytest.raises(TaskCommandContractError, match="identity mismatch"):
        await read_task_read_model_scan_records(
            redis, environment="development", run_id=RUN_ID, parser_type="t_fullchain",
            records=[{"task_id": TASK_ID, "indexed_revision": 3}],
        )


class RedisScanStub:
    def __init__(self):
        self.zsets = {}
        self.values = {}
        self.commands = []
        self.ranges = []
        self.expirations = []
        self.deleted = []

    async def execute_command(self, *args):
        self.commands.append(args)
        command, destination = args[0], args[1]
        if command == "ZUNIONSTORE":
            self.zsets[destination] = [("t_fullchain:17", 4.0)]
            return len(self.zsets[destination])
        if command == "ZINTERSTORE":
            self.zsets[destination] = [
                ("t_fullchain:17", 4.0),
                ("t_fullchain:18", 2.0),
                ("t_fullchain:19", 1.0),
            ]
            return len(self.zsets[destination])
        raise AssertionError(command)

    async def zcard(self, key):
        return len(self.zsets.get(key, []))

    async def set(self, key, value, *, ex):
        self.values[key] = value
        self.expirations.append((key, ex))
        return True

    async def get(self, key):
        return self.values.get(key)

    async def expire(self, key, ttl):
        self.expirations.append((key, ttl))
        return key in self.zsets or key in self.values

    async def zrange(self, key, start, end, *, withscores):
        assert withscores is True
        self.ranges.append(("ZRANGE", start, end))
        ordered = sorted(self.zsets[key], key=lambda item: (item[1], item[0]))
        return ordered[start:end + 1]

    async def zrevrange(self, key, start, end, *, withscores):
        assert withscores is True
        self.ranges.append(("ZREVRANGE", start, end))
        ordered = sorted(self.zsets[key], key=lambda item: (item[1], item[0]), reverse=True)
        return ordered[start:end + 1]

    async def delete(self, *keys):
        self.deleted.extend(keys)
        for key in keys:
            self.values.pop(key, None)
            self.zsets.pop(key, None)
        return len(keys)


class RedisGroupStub:
    def __init__(self):
        self.zsets = {}
        self.values = {}
        self.commands = []
        self.rows = {}

    async def execute_command(self, *args):
        self.commands.append(args)
        command = args[0]
        if command == "ZINTERSTORE":
            self.zsets[args[1]] = [(ROW_GROUP_TOKEN, 1.0)]
            return 1
        if command == "ZUNIONSTORE":
            self.zsets[args[1]] = [(ROW_GROUP_TOKEN, 1.0)]
            return 1
        if command == "ZDIFFSTORE":
            self.zsets[args[1]] = [(ROW_GROUP_TOKEN, 1.0)]
            return 1
        if command == "ZADD":
            return 1
        raise AssertionError(command)

    async def zcard(self, key):
        return len(self.zsets.get(key, []))

    async def zrange(self, key, start, end, *args, **kwargs):
        entries = self.zsets.get(key, [])
        if end == -1:
            end = len(entries) - 1
        if kwargs.get("withscores"):
            return entries[start:end + 1]
        return [entry[0] for entry in entries[start:end + 1]]

    async def smembers(self, _key):
        return {search_token("社区甲", field="group-community", environment="development", secret=SECRET)}

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, *keys):
        for key in keys:
            self.zsets.pop(key, None)
        return len(keys)

    def pipeline(self, *, transaction):
        assert transaction is False
        return RedisPipelineStub(self)


@pytest.mark.asyncio
async def test_group_page_filters_any_out_of_scope_member_before_counting():
    redis = RedisGroupStub()
    result = await query_task_read_model_group_ids(
        redis, "development", RUN_ID, "t_fullchain", {}, ["社区甲"], None, 0, 10,
    )
    assert result == {"total": 1, "group_tokens": [ROW_GROUP_TOKEN]}
    assert any(command[0] == "ZDIFFSTORE" for command in redis.commands)
    assert all("社区甲" not in repr(command) for command in redis.commands)


@pytest.mark.asyncio
async def test_group_page_intersects_hmac_state_index_after_readiness_gate():
    redis = RedisGroupStub()
    prefix = f"binhu:task-rm:v4:development:{RUN_ID}"
    redis.values[f"{prefix}:state-index-version"] = "v1"

    result = await query_task_read_model_group_ids(
        redis, "development", RUN_ID, "t_fullchain", {}, None, None, 0, 10,
        state_filters={"qmf.state": ["completed_mismatch"]},
    )

    assert result == {"total": 1, "group_tokens": [ROW_GROUP_TOKEN]}
    assert any(
        command[0] == "ZINTERSTORE"
        and any(
            isinstance(arg, str) and arg.startswith(f"{prefix}:gidx-state:v1:")
            for arg in command
        )
        for command in redis.commands
    )
    assert all("completed_mismatch" not in repr(command) for command in redis.commands)


@pytest.mark.asyncio
async def test_state_facet_count_uses_canonical_group_index_without_payload_scan():
    redis = RedisGroupStub()
    prefix = f"binhu:task-rm:v4:development:{RUN_ID}"
    redis.values[f"{prefix}:state-index-version"] = "v1"

    count = await count_task_read_model_groups(
        redis, "development", RUN_ID, "t_fullchain", {}, None, None,
        state_filters={"qmf.state": ["completed_mismatch"]},
    )

    assert count == 1
    intersections = [command for command in redis.commands if command[0] == "ZINTERSTORE"]
    assert len(intersections) == 1
    assert any(
        isinstance(value, str) and value.startswith(f"{prefix}:gidx-state:v1:")
        for value in intersections[0]
    )
    assert all("completed_mismatch" not in repr(command) for command in redis.commands)


@pytest.mark.asyncio
async def test_group_page_does_not_use_unverified_state_indexes():
    redis = RedisGroupStub()
    with pytest.raises(TaskCommandContractError, match="state indexes are not ready"):
        await query_task_read_model_group_ids(
            redis, "development", RUN_ID, "t_fullchain", {}, None, None, 0, 10,
            state_filters={"qmf.state": ["completed_mismatch"]},
        )


@pytest.mark.asyncio
async def test_group_record_read_orders_sources_and_calculates_conflict():
    redis = RedisGroupStub()
    task_ids = ["t_fullchain:17", "t_fullchain:18"]
    for index, task_id in enumerate(task_ids):
        event_id = str(uuid.uuid4())
        name = "Canonical" if index == 0 else "Different"
        envelope = encrypt_task_snapshot(
            {"姓名": name, "社区": "社区甲"}, environment="development",
            task_id=task_id, revision=1, event_id=event_id,
            command_id=COMMAND_ID, row_group_token=ROW_GROUP_TOKEN,
        )
        key = task_read_model_keys("development", RUN_ID, task_id)[0]
        redis.rows[key] = {
            **envelope, "environment": "development", "run_id": RUN_ID,
            "task_id": task_id, "source_id": str(17 + index), "revision": "1",
            "event_id": event_id, "command_id": COMMAND_ID, "tombstone": "0",
            "row_group_token": ROW_GROUP_TOKEN,
        }
    redis.zsets[f"binhu:task-rm:v4:development:{RUN_ID}:group:{ROW_GROUP_TOKEN}:members"] = [
        ("0000000000000000017|t_fullchain:17", 0.0),
        ("0000000000000000018|t_fullchain:18", 0.0),
    ]
    result = await read_task_read_model_group_records(
        redis, "development", RUN_ID, "t_fullchain", [ROW_GROUP_TOKEN],
    )
    assert result[0]["source_id"] == 17
    assert [source["source_id"] for source in result[0]["sources"]] == [17, 18]
    assert result[0]["source_count"] == 2
    assert result[0]["conflict"] is True


@pytest.mark.asyncio
async def test_read_model_scan_streams_a_stable_bounded_page_and_cleans_up():
    redis = RedisScanStub()
    scan = await create_task_read_model_scan(
        redis, environment="development", run_id=RUN_ID, parser_type="t_fullchain",
        communities=["合成社区"], filters={"核查结果": ["待核查"]},
        search="合成姓名", secret=SECRET, descending=True,
    )
    assert scan["total"] == 3
    assert scan["cursor"] == 0
    assert scan["scan_id"]
    assert all("合成社区" not in repr(command) for command in redis.commands)
    assert all(command[-2:] == ("AGGREGATE", "MIN") for command in redis.commands)

    first = await read_task_read_model_scan(
        redis, environment="development", run_id=RUN_ID, parser_type="t_fullchain",
        scan_id=scan["scan_id"], cursor=0, limit=2,
    )
    second = await read_task_read_model_scan(
        redis, environment="development", run_id=RUN_ID, parser_type="t_fullchain",
        scan_id=scan["scan_id"], cursor=first["next_cursor"], limit=2,
    )
    assert first["records"] == [
        {"task_id": "t_fullchain:17", "indexed_revision": 4},
        {"task_id": "t_fullchain:18", "indexed_revision": 2},
    ]
    assert first["next_cursor"] == 2
    assert second["records"] == [{"task_id": "t_fullchain:19", "indexed_revision": 1}]
    assert second["next_cursor"] is None
    assert all(end - start + 1 <= 2 for _command, start, end in redis.ranges)

    await close_task_read_model_scan(
        redis, environment="development", run_id=RUN_ID, parser_type="t_fullchain",
        scan_id=scan["scan_id"],
    )
    assert len(redis.deleted) == 2


@pytest.mark.asyncio
async def test_read_model_scan_rejects_oversized_page_wrong_identity_and_expired_ttl():
    redis = RedisScanStub()
    scan = await create_task_read_model_scan(
        redis, environment="development", run_id=RUN_ID, parser_type="t_fullchain",
        communities=None, filters={}, search=None, secret=SECRET,
    )
    with pytest.raises(TaskCommandContractError, match="scan page"):
        await read_task_read_model_scan(
            redis, environment="development", run_id=RUN_ID, parser_type="t_fullchain",
            scan_id=scan["scan_id"], cursor=0, limit=TASK_READ_MODEL_SCAN_BATCH_MAX + 1,
        )
    with pytest.raises(TaskCommandContractError, match="scan expired"):
        await read_task_read_model_scan(
            redis, environment="development", run_id=RUN_ID, parser_type="t_rental_check",
            scan_id=scan["scan_id"], cursor=0, limit=10,
        )

    _task_key, _indexes_key, prefix = task_read_model_keys(
        "development", RUN_ID, "t_fullchain:1",
    )
    metadata_key = f"{prefix}:scan-meta:t_fullchain:{scan['scan_id']}"
    redis.values.pop(metadata_key, None)
    with pytest.raises(TaskCommandContractError, match="scan expired"):
        await read_task_read_model_scan(
            redis, environment="development", run_id=RUN_ID, parser_type="t_fullchain",
            scan_id=scan["scan_id"], cursor=0, limit=10,
        )
