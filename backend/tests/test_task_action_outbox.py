"""Action classification must survive the actual authoritative save transaction."""
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers import mobile_tasks, query


class Cursor:
    rowcount = 1

    def __init__(self, fail_delivery=False):
        self.calls = []
        self.fail_delivery = fail_delivery
        self.last_sql = ""
        self.delivery = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, params=None):
        self.last_sql = sql
        self.calls.append((sql, params))
        if "INSERT INTO _kafka_event_delivery" in sql:
            if self.fail_delivery:
                raise RuntimeError("fixture ledger unavailable")
            self.delivery = params

    async def fetchone(self):
        if "SELECT run_id,payload_sha256" in self.last_sql:
            return self.delivery[1], self.delivery[3]
        return (27,)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["online.task.claimed", "online.task.reviewed", "online.task.changed"])
@pytest.mark.parametrize("fail_delivery", [False, True])
async def test_action_event_and_ledger_share_business_transaction(monkeypatch, event_type, fail_delivery):
    cur = Cursor(fail_delivery)
    conn = SimpleNamespace(cursor=lambda: cur, begin=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    parser = SimpleNamespace(
        COLUMNS=["核查结果"], COMMUNITY_COLUMN="社区", table_name="t_fullchain",
        community_value=lambda values: "", get_business_key=lambda: [],
        make_row_key=lambda values: "fixture-row",
    )
    source = {
        "id": 904, "physical_row": 27, "spreadsheet_id": 0,
        "source_kind": "local_table", "source_ref": "t_fullchain:27",
        "row_key": "fixture-row", "revision": 4, "values": {"核查结果": ""},
    }
    monkeypatch.setattr(query, "get_parser", lambda name: parser)
    monkeypatch.setattr(query, "local_data_source_enabled", lambda: True)
    monkeypatch.setattr(query, "_load_source_row", AsyncMock(return_value=source))
    monkeypatch.setattr(query, "_managed_column_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(query, "_insert_writeback_audit", AsyncMock(return_value=1))
    for name in ("validate_row_changes", "migrate_responsibility_row_key", "update_lightweight_projection", "enqueue_projection_jobs"):
        monkeypatch.setattr(query, name, AsyncMock())
    monkeypatch.setattr(query, "task_update_is_credited_to", AsyncMock(return_value=False))
    monkeypatch.setattr(query.settings, "KAFKA_TASK_EVENTS_ENABLED", True)
    monkeypatch.setattr(query.settings, "APP_ENVIRONMENT", "shadow")
    monkeypatch.setattr(query.settings, "LOAD_TEST_RUN_ID", "KSHADOW-action-test")

    async def callback(**kwargs):
        assert kwargs["cur"] is cur
        conn.commit.assert_not_awaited()
        await cur.execute("UPDATE fixture_review_flow SET revision=5")

    kwargs = dict(
        parser_type="全链条", source_id=904, changes={"核查结果": "无法核实"},
        expected_revision=4, request=object(), user={"id": 1}, conn=conn,
        record_unverifiable_save=False, transaction_callback=callback,
        task_event_type=event_type,
    )
    if fail_delivery:
        with pytest.raises(RuntimeError, match="fixture ledger unavailable"):
            await query.queue_source_fields(**kwargs)
        conn.rollback.assert_awaited_once()
        conn.commit.assert_not_awaited()
    else:
        result = await query.queue_source_fields(**kwargs)
        assert result["revision"] == 5
        conn.commit.assert_awaited_once()
        conn.rollback.assert_not_awaited()
        payload = json.loads(cur.delivery[2])
        assert payload["event_type"] == {"online.task.changed": "task.saved"}.get(event_type, event_type.removeprefix("online."))
        assert (payload["task_id"], payload["source_id"], payload["revision"]) == ("t_fullchain:27", 904, 5)
        assert payload["changed_fields"] == ["check_result"]
        assert "无法核实" not in cur.delivery[2]
    sqls = [sql for sql, params in cur.calls]
    domain_writes = [sql for sql in sqls if "INSERT INTO `_domain_event_outbox`" in sql]
    assert len(domain_writes) == 1
    assert sqls.index("UPDATE fixture_review_flow SET revision=5") < sqls.index(domain_writes[0])


@pytest.mark.asyncio
async def test_compatible_analysis_route_classifies_the_existing_save(monkeypatch):
    """The remaining analysis route uses the same save/Outbox, without a second event."""
    cur = Cursor()
    cur.fetchone = AsyncMock(return_value=(json.dumps({"研判": ""}),))
    conn = SimpleNamespace(cursor=lambda: cur)
    queued = AsyncMock(return_value={"revision": 5})
    monkeypatch.setattr(mobile_tasks, "supports_unverifiable_review", lambda parser_type: False)
    monkeypatch.setattr(mobile_tasks, "_require_analysis_user", lambda user: user)
    monkeypatch.setitem(mobile_tasks.TASK_WORKFLOWS, "全链条", SimpleNamespace(
        analysis_fields=["研判"], review_stage=lambda values: "waiting_analysis",
    ))
    monkeypatch.setattr(mobile_tasks, "queue_source_fields", queued)
    await mobile_tasks.update_mobile_task_analysis(
        "全链条", 904,
        mobile_tasks.TaskBatchUpdate(changes={"研判": "虚构意见"}, expected_revision=4),
        request=object(), user={"id": 1}, conn=conn,
    )
    queued.assert_awaited_once()
    assert queued.await_args.kwargs["task_event_type"] == "online.task.reviewed"
    assert queued.await_args.kwargs["conn"] is conn


@pytest.mark.asyncio
async def test_save_rejects_unrelated_event_classification_before_transaction(monkeypatch):
    conn = SimpleNamespace(begin=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(query, "local_data_source_enabled", lambda: True)
    with pytest.raises(ValueError, match="unsupported task save event type"):
        await query.queue_source_fields(
            parser_type="全链条", source_id=904, changes={"核查结果": "无法核实"},
            expected_revision=4, request=object(), user={}, conn=conn,
            task_event_type="online.task.archived",
        )
    conn.begin.assert_not_awaited()
