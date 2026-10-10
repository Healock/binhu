from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook

from tests.test_household_cancellation_status import Connection
from routers import registry_extended as extended
from services.registry_import import classify_household_file_rows


def upload(name, *, number="H1", address="合成路1号", kind="个人租赁", status=None, empty=False):
    book = Workbook()
    book.active.append(["出租屋地址", "住房类型", "社区名称", "户号", *(["是否注销"] if status is not None else [])])
    if not empty:
        book.active.append([address, kind, "合成社区", number, *([status] if status is not None else [])])
    result = BytesIO()
    book.save(result)
    return UploadFile(filename=name, file=BytesIO(result.getvalue()))


@pytest.fixture
def capture(monkeypatch):
    mock = AsyncMock(return_value={"batch_id": 1})
    monkeypatch.setattr(extended, "_preview_household_rows", mock)
    return mock


@pytest.mark.asyncio
async def test_ten_type_state_groups_are_one_batch_with_provenance(capture):
    files, options = [], []
    for kind in ["个人租赁", "单位租赁", "自购房屋", "借住", "其他"]:
        for state in ["cancelled", "not_cancelled"]:
            index = len(files) + 1
            files.append(upload(f"synthetic-{index}.xlsx", number=f"H{index}", address=f"合成路{index}号", kind=kind))
            options.append({"housing_type": kind, "household_status": state, "expected_count": 1})
    await extended.preview_household_files(None, files, json.dumps(options), {"id": 7}, None)
    _, rows, classified, _, _, _, _, summary = capture.call_args.args
    assert summary["file_count"] == 10 and summary["unique_household_count"] == 10
    assert summary["household_status_counts"] == {"cancelled": 5, "not_cancelled": 5, "unknown": 0}
    assert classified["normal_count"] == 10 and classified["issue_count"] == 0
    assert len({extended._household_source_ref(row) for row in rows}) == 10
    assert all(row["source_filter"] and len(row["source_file_sha256"]) == 64 for row in rows)
    assert classified["rows"][0]["housing_type"] == "个人出租"
    assert classified["rows"][0]["source_housing_type"] == "个人租赁"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,option", [("housing_type", "借住"), ("household_status", "not_cancelled"), ("expected_count", 30001)])
async def test_mislabeled_or_truncated_file_is_rejected_before_database(capture, field, option):
    with pytest.raises(HTTPException) as error:
        await extended.preview_household_files(None, [upload("synthetic.xlsx", status="是")], json.dumps([{field: option}]), {"id": 7}, None)
    assert error.value.status_code == 422
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_yes_no_headers_and_empty_groups(capture):
    await extended.preview_household_files(None, [upload("yes.xlsx", status="是"), upload("empty.xlsx", empty=True)],
                                           json.dumps([{}, {"household_status": "not_cancelled", "expected_count": 0}]), {"id": 7}, None)
    summary = capture.call_args.args[-1]
    assert summary["files"][1]["total_count"] == 0
    assert summary["household_status_counts"]["cancelled"] == 1


@pytest.mark.asyncio
async def test_input_limits_and_invalid_options(capture):
    for files, options in [([], "[]"), ([upload("synthetic.xlsx")] * 11, "[]"), ([upload("synthetic.xlsx")], "{}"),
                           ([upload("synthetic.xlsx")], '[{"household_status":"wrong"}]')]:
        with pytest.raises(HTTPException) as error:
            await extended.preview_household_files(None, files, options, {"id": 7}, None)
        assert error.value.status_code == 422
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_reordered_files_keep_same_batch_identity_but_state_changes_do_not(capture):
    contents = []
    workbooks = {i: await upload(f"synthetic-{i}.xlsx", number=f"H{i}", address=f"合成路{i}号").read() for i in [1, 2]}
    for state, order in [("cancelled", [1, 2]), ("cancelled", [2, 1]), ("not_cancelled", [1, 2])]:
        files = [UploadFile(filename=f"synthetic-{i}.xlsx", file=BytesIO(workbooks[i])) for i in order]
        await extended.preview_household_files(None, files, json.dumps([{"household_status": state}] * 2), {"id": 7}, None)
        contents.append(capture.call_args.args[3])
    assert contents[0] == contents[1] and contents[0] != contents[2]


def row(ref, **changes):
    return {"house_no": "H1", "community": "合成社区", "address": "合成路1号", "housing_type": "个人出租",
            "household_status": "已注销", "source_row": 2, "import_source_ref": ref, **changes}


def test_identical_household_is_suppressed_but_sources_are_preserved():
    result = classify_household_file_rows([row("a"), row("b")])
    assert result["normal_count"] == 1 and result["duplicate_row_count"] == 1
    assert len(result["rows"]) == 2 and result["issue_count"] == 0
    assert result["rows"][1]["import_skip"] == "identical_household"


@pytest.mark.parametrize("changes", [{"address": "合成路2号"}, {"household_status": "未注销"}, {"housing_type": "借住"}])
def test_conflicting_household_cannot_overwrite_in_upload_order(changes):
    result = classify_household_file_rows([row("a"), row("b", **changes)])
    assert result["normal_count"] == 0
    assert {item["payload"]["import_source_ref"] for item in result["issues"]} == {"a", "b"}


def test_distinct_household_ids_at_same_address_are_not_deduplicated():
    result = classify_household_file_rows([row("a"), row("b", house_no="H2")])
    assert result["normal_count"] == 2 and result["duplicate_row_count"] == 0
    assert result["issue_count"] == 0


def test_different_unknown_codes_and_missing_status_are_not_identical_sources():
    result = classify_household_file_rows([row("a", household_status="0"), row("b", household_status="1")])
    assert result["normal_count"] == 0 and result["duplicate_row_count"] == 0
    absent = row("a")
    del absent["household_status"]
    result = classify_household_file_rows([absent, row("b", household_status="")])
    assert result["normal_count"] == 0 and result["duplicate_row_count"] == 0


@pytest.mark.asyncio
async def test_confirmation_skips_suppressed_sources_and_preserves_original_transaction(monkeypatch):
    conn = Connection(payload=row("a", import_skip="identical_household"))
    monkeypatch.setattr(extended, "record_admin_audit", AsyncMock())
    monkeypatch.setattr(extended, "request_audit_fields", lambda *_: {})
    result = await extended.confirm_household_import(1, None, {"id": 7}, conn)
    assert result["imported_count"] == 0 and conn.committed
    assert not any(sql.startswith("UPDATE registry_properties SET") for sql, _ in conn.calls)


def test_full_66267_synthetic_rows_keep_cancellation_counts_and_no_conflicts():
    rows = [row(f"source-{i}", house_no=f"H{i}", address=f"合成路{i}号",
                household_status="已注销" if i < 28104 else "未注销") for i in range(66267)]
    result = classify_household_file_rows(rows)
    assert result["normal_count"] == 66267 and result["issue_count"] == 0
    assert sum(item["household_status"] == "已注销" for item in result["normal_rows"]) == 28104
    assert sum(item["household_status"] == "未注销" for item in result["normal_rows"]) == 38163


@pytest.mark.asyncio
async def test_unknown_source_code_cannot_be_overwritten_by_declared_state(capture):
    with pytest.raises(HTTPException) as error:
        await extended.preview_household_files(None, [upload("synthetic.xlsx", status="1")],
                                               '[{"household_status":"cancelled"}]', {"id": 7}, None)
    assert error.value.status_code == 422
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_status_counts_do_not_claim_conflicts_are_importable(capture):
    await extended.preview_household_files(None, [upload("yes.xlsx", status="是"), upload("no.xlsx", number="H2", status="否")],
                                           "[{},{}]", {"id": 7}, None)
    summary = capture.call_args.args[-1]
    assert summary["household_status_counts"] == {"cancelled": 1, "not_cancelled": 1, "unknown": 0}
    assert summary["importable_status_counts"] == {"cancelled": 1, "not_cancelled": 1, "unknown": 0}


@pytest.mark.asyncio
async def test_preview_sources_and_issues_commit_together_and_roll_back_on_failure(monkeypatch):
    conn = Connection()
    conn.lastrowid = 123
    conn.fetchone = AsyncMock(return_value=None)
    monkeypatch.setattr(extended, "record_admin_audit", AsyncMock())
    monkeypatch.setattr(extended, "request_audit_fields", lambda *_: {})
    source_rows = [row("file-a:2"), row("file-b:2", household_status="未注销")]
    classified = classify_household_file_rows(source_rows)
    result = await extended._preview_household_rows(None, source_rows, classified, "a" * 64, "synthetic-files", {"id": 7}, conn, {"file_count": 2})
    assert conn.committed and result["batch_id"] == 123 and result["file_count"] == 2
    saved = next(values for sql, values in conn.calls if sql.startswith("INSERT INTO registry_source_records"))
    assert {values[1] for values in saved} == {"file-a:2", "file-b:2"}
    issue_rows = next(values for sql, values in conn.calls if sql.startswith("INSERT INTO registry_import_issues"))
    assert len(issue_rows) == 2
    assert not any("INSERT INTO registry_properties" in sql for sql, _ in conn.calls)

    broken = Connection()
    broken.lastrowid = 124
    broken.fetchone = AsyncMock(return_value=None)
    monkeypatch.setattr(extended, "_bulk_insert_import_issues", AsyncMock(side_effect=RuntimeError("synthetic failure")))
    with pytest.raises(RuntimeError):
        await extended._preview_household_rows(None, source_rows, classified, "b" * 64, "synthetic-files", {"id": 7}, broken)
    assert broken.rolled_back and not broken.committed


def test_multi_file_route_has_same_import_permission_as_single_file():
    for path in ["/api/registry/imports/households/preview", "/api/registry/imports/households/files/preview"]:
        route = next(route for route in extended.router.routes if route.path == path)
        dependency = next(dep.call for dep in route.dependant.dependencies if dep.name == "user")
        assert "registry.import.manage" in str([cell.cell_contents for cell in dependency.__closure__])


class IdentityConnection(Connection):
    def __init__(self, *, sources, properties=(), newer_ids=(), previous_count=0):
        super().__init__()
        self.sources = sources
        self.properties = properties
        self.newer_ids = newer_ids
        self.previous_count = previous_count

    async def fetchone(self):
        if self.sql.startswith("SELECT status, imported_count FROM"):
            return ("partially_imported" if self.previous_count else "preview", self.previous_count)
        return await super().fetchone()

    async def fetchall(self):
        if self.sql.startswith("SELECT id, source_ref, payload_json"):
            return [(index, f"source-{index}", json.dumps(payload)) for index, payload, entity_id in self.sources
                    if entity_id is None or "entity_id IS NULL" not in self.sql]
        if self.sql.startswith("SELECT id, community_id, street"):
            return self.properties
        if self.sql.startswith("SELECT DISTINCT entity_id"):
            return [(item,) for item in self.newer_ids]
        if self.sql.startswith("SELECT id, source_ref FROM registry_properties"):
            return [(100 + index, f"source-{index}")
                    for index, payload, entity_id in self.sources if entity_id is None]
        return []


def existing_property(identifier=42, *, number="H1", address="合成路1号"):
    return (identifier, 8, "", address, "", "", extended.normalize_address(address), number)


@pytest.fixture
def confirmation_dependencies(monkeypatch):
    monkeypatch.setattr(extended, "_household_import_community", AsyncMock(return_value=(8, "合成社区")))
    monkeypatch.setattr(extended, "record_admin_audit", AsyncMock())
    monkeypatch.setattr(extended, "request_audit_fields", lambda *_: {})


@pytest.mark.asyncio
@pytest.mark.parametrize("properties,payload", [
    ([existing_property(address="合成路2号")], row("a")),
    ([existing_property(), existing_property(43)], row("a")),
])
async def test_existing_household_identity_conflict_never_changes_status(confirmation_dependencies, properties, payload):
    conn = IdentityConnection(sources=[(1, payload, None)], properties=properties)
    result = await extended.confirm_household_import(10, None, {"id": 7}, conn)
    assert result["imported_count"] == 0 and conn.committed
    assert not any(sql.startswith("UPDATE registry_properties SET") for sql, _ in conn.calls)
    assert not any(sql.startswith("INSERT INTO registry_properties") and values for sql, values in conn.calls)
    issues = [values for sql, values in conn.calls if sql.startswith("INSERT INTO registry_import_issues")]
    assert sum(map(len, issues)) == 1


@pytest.mark.asyncio
async def test_matching_household_updates_correct_property_and_counts(confirmation_dependencies):
    conn = IdentityConnection(sources=[(1, row("a", household_status="未注销"), None)], properties=[existing_property()])
    result = await extended.confirm_household_import(10, None, {"id": 7}, conn)
    assert result["updated_count"] == 1 and result["inserted_count"] == 0
    _, values = next((sql, values) for sql, values in conn.calls if sql.startswith("UPDATE registry_properties SET"))
    assert values[5] == "H1" and values[-1] == 42 and values[-5:-2] == ("未注销",) * 3


@pytest.mark.asyncio
async def test_missing_number_does_not_erase_existing_household_identity(confirmation_dependencies):
    conn = IdentityConnection(sources=[(1, row("a", house_no=""), None)], properties=[existing_property()])
    await extended.confirm_household_import(10, None, {"id": 7}, conn)
    _, values = next((sql, values) for sql, values in conn.calls if sql.startswith("UPDATE registry_properties SET"))
    assert values[5] == "H1"


@pytest.mark.asyncio
async def test_partial_retry_skips_successful_sources_and_keeps_cumulative_count(confirmation_dependencies):
    conn = IdentityConnection(sources=[(1, row("a", household_status="未注销"), 42)],
                              properties=[existing_property()], previous_count=1)
    result = await extended.confirm_household_import(10, None, {"id": 7}, conn)
    assert result["imported_count"] == 0 and result["total_imported_count"] == 1
    assert not any(sql.startswith("UPDATE registry_properties SET") for sql, _ in conn.calls)
    _, values = next((sql, values) for sql, values in conn.calls if sql.startswith("UPDATE registry_source_batches SET"))
    assert values[1] == 1


@pytest.mark.asyncio
async def test_older_batch_cannot_overwrite_newer_successful_source(confirmation_dependencies):
    conn = IdentityConnection(sources=[(1, row("a", household_status="未注销"), None)],
                              properties=[existing_property()], newer_ids=[42])
    result = await extended.confirm_household_import(10, None, {"id": 7}, conn)
    assert result["imported_count"] == 0
    assert not any(sql.startswith("UPDATE registry_properties SET") for sql, _ in conn.calls)
    issues = next(values for sql, values in conn.calls if sql.startswith("INSERT INTO registry_import_issues") and values)
    assert "更新批次" in issues[0][-1]


@pytest.mark.asyncio
async def test_aliases_resolving_to_same_community_cannot_duplicate_household(confirmation_dependencies):
    conn = IdentityConnection(sources=[(1, row("a"), None), (2, row("b", community="合成别名", address="合成路2号"), None)])
    result = await extended.confirm_household_import(10, None, {"id": 7}, conn)
    assert result["imported_count"] == 0
    issues = next(values for sql, values in conn.calls if sql.startswith("INSERT INTO registry_import_issues") and values)
    assert len(issues) == 2


@pytest.mark.asyncio
async def test_new_household_insert_retains_declared_status(confirmation_dependencies):
    conn = IdentityConnection(sources=[(1, row("a"), None)])
    result = await extended.confirm_household_import(10, None, {"id": 7}, conn)
    assert result["inserted_count"] == 1 and result["updated_count"] == 0
    _, values = next((sql, values) for sql, values in conn.calls if sql.startswith("INSERT INTO registry_properties"))
    assert values[0][-3] == "已注销"


def test_same_address_cancellation_history_and_cross_community_are_normal():
    result = classify_household_file_rows([
        row("a"), row("b", house_no="H2", household_status="未注销"),
        row("c", community="另一合成社区"),
    ])
    assert result["normal_count"] == 3 and result["issue_count"] == 0
    assert [item["household_status"] for item in result["normal_rows"]] == ["已注销", "未注销", "已注销"]


def test_five_household_conflicts_count_ten_rows_once():
    sources = [row(str(i), house_no=f"H{i}", address="共享合成地址") for i in range(200)]
    sources += [row(f"conflict-{i}", house_no=f"H{i}", address="共享合成地址", household_status="未注销") for i in range(5)]
    result = classify_household_file_rows(sources)
    assert result["issue_count"] == len(result["issues"]) == 10
    assert result["normal_count"] == 195 and result["duplicate_groups"] == 5


@pytest.mark.asyncio
async def test_same_address_distinct_households_get_distinct_source_links(confirmation_dependencies):
    conn = IdentityConnection(sources=[(1, row("a"), None), (2, row("b", house_no="H2", household_status="未注销"), None)])
    result = await extended.confirm_household_import(10, None, {"id": 7}, conn)
    assert result["inserted_count"] == 2
    links = [values for sql, values in conn.calls if sql.startswith("UPDATE registry_source_records SET entity_id")]
    assert links == [[(101, 1), (102, 2)]]
    inserts = next(values for sql, values in conn.calls if sql.startswith("INSERT INTO registry_properties"))
    assert [item[-3] for item in inserts] == ["已注销", "未注销"]


@pytest.mark.asyncio
async def test_same_address_other_household_does_not_block_exact_update(confirmation_dependencies):
    conn = IdentityConnection(sources=[(1, row("a"), None)], properties=[existing_property(), existing_property(43, number="H2")])
    result = await extended.confirm_household_import(10, None, {"id": 7}, conn)
    assert result["updated_count"] == 1
    values = next(values for sql, values in conn.calls if sql.startswith("UPDATE registry_properties SET"))
    assert values[-1] == 42


@pytest.mark.asyncio
async def test_same_address_other_household_is_never_overwritten(confirmation_dependencies):
    conn = IdentityConnection(sources=[(1, row("a"), None)], properties=[existing_property(number="H2")])
    result = await extended.confirm_household_import(10, None, {"id": 7}, conn)
    assert result["inserted_count"] == 1 and result["updated_count"] == 0
    assert not any(sql.startswith("UPDATE registry_properties SET") for sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_multiple_households_do_not_claim_single_unnumbered_legacy_property(confirmation_dependencies):
    conn = IdentityConnection(sources=[(1, row("a"), None), (2, row("b", house_no="H2"), None)],
                              properties=[existing_property(number="")])
    result = await extended.confirm_household_import(10, None, {"id": 7}, conn)
    assert result["inserted_count"] == 2 and result["updated_count"] == 0


@pytest.mark.asyncio
async def test_numberless_source_at_multiple_households_remains_blocked(confirmation_dependencies):
    conn = IdentityConnection(sources=[(1, row("a", house_no=""), None)],
                              properties=[existing_property(), existing_property(43, number="H2")])
    result = await extended.confirm_household_import(10, None, {"id": 7}, conn)
    assert result["imported_count"] == 0
