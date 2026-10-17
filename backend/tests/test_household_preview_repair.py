import json
import pytest
from services.household_preview_repair import LEGACY_REASONS, repair_household_preview
from tests.test_household_multi_file_import import row


class Cursor:
    def __init__(self, status="preview", imported=0, linked=0):
        self.status, self.imported, self.linked = status, imported, linked
        self.issues = [(str(i), "household_duplicate", "pending", LEGACY_REASONS[0]) for i in range(200)]
        self.rows = [row(str(i), house_no=f"H{i}", address="共享合成地址") for i in range(200)]
        self.rows += [row(str(200+i), house_no=f"H{i}", address="共享合成地址", household_status="未注销") for i in range(5)]
        self.calls = []

    async def execute(self, sql, params=()):
        self.sql, self.params = sql, params
        self.calls.append((sql, params))
        if sql.startswith("UPDATE registry_import_issues"):
            self.issues = [(ref, kind, "superseded" if state == "pending" and reason in LEGACY_REASONS else state, reason)
                           for ref, kind, state, reason in self.issues]

    async def executemany(self, sql, params):
        self.calls.append((sql, params))
        self.issues += [(item[3], item[1], "pending", item[-1]) for item in params]

    async def fetchone(self):
        if self.sql.startswith("SELECT status"):
            return self.status, self.imported
        if "entity_id IS NOT NULL" in self.sql:
            return self.linked,
        if "COUNT(DISTINCT source_ref)" in self.sql:
            return len({ref for ref, _, state, _ in self.issues if state == "pending"}),
        return sum(state == "pending" and reason in LEGACY_REASONS for _, _, state, reason in self.issues),

    async def fetchall(self):
        if "FROM registry_source_records" in self.sql:
            return [(i, str(i), json.dumps(item)) for i, item in enumerate(self.rows)]
        return [(ref, kind) for ref, kind, state, _ in self.issues if state in {"pending", "resolved", "dismissed"}]


@pytest.mark.asyncio
async def test_repair_preserves_obsolete_evidence_and_leaves_ten_real_issues_idempotently():
    cur = Cursor()
    result = await repair_household_preview(cur, 19)
    assert result == {"batch_id": 19, "normal_count": 195, "issue_count": 10}
    assert sum(state == "superseded" for _, _, state, _ in cur.issues) == 200
    assert sum(state == "pending" for _, _, state, _ in cur.issues) == 10
    assert await repair_household_preview(cur, 19) is None
    assert not any("DELETE" in sql or "UPDATE registry_properties" in sql or "UPDATE registry_source_records" in sql for sql, _ in cur.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,imported,linked", [("imported", 205, 205), ("cancelled", 0, 0)])
async def test_repair_never_changes_imported_or_linked_batches(status, imported, linked):
    cur = Cursor(status, imported, linked)
    assert await repair_household_preview(cur, 19) is None
    assert not any(sql.startswith("UPDATE") or sql.startswith("INSERT") for sql, _ in cur.calls)


@pytest.mark.asyncio
async def test_repair_retains_manual_decisions_and_unrelated_pending_issues():
    cur = Cursor()
    cur.issues += [("manual", "household_duplicate", "resolved", LEGACY_REASONS[0]),
                   ("25", "household_community_unresolved", "pending", "unresolved")]
    result = await repair_household_preview(cur, 19)
    assert result["issue_count"] == 11
    assert ("manual", "household_duplicate", "resolved", LEGACY_REASONS[0]) in cur.issues
    assert ("25", "household_community_unresolved", "pending", "unresolved") in cur.issues


@pytest.mark.asyncio
async def test_partial_batch_reclassification_never_changes_imported_properties_or_source_links():
    cur = Cursor("partially_imported", 195, 195)
    result = await repair_household_preview(cur, 19)
    assert result["issue_count"] == 10
    assert not any("UPDATE registry_properties" in sql or "UPDATE registry_source_records" in sql
                   or "imported_count=" in sql for sql, _ in cur.calls)


@pytest.mark.asyncio
async def test_manually_resolved_real_conflict_is_not_reopened():
    cur = Cursor()
    cur.issues += [("0", "household_duplicate", "resolved", "manually checked")]
    result = await repair_household_preview(cur, 19)
    assert result["issue_count"] == 9
    assert not any(ref == "0" and state == "pending" for ref, _, state, _ in cur.issues)
