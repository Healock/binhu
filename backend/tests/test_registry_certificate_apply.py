"""Synthetic transaction tests, not a substitute for real MySQL acceptance."""

import copy
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services import registry_certificate_apply as service
from services.registry_certificate_source import certificate_source_ref, certificate_content_hash


def notice(*, source_id=1, signed="否", **extra):
    row = {"id": source_id, "community": "测试社区", "address": "合成测试路1号",
           "sjczrxm": "合成责任人", "isSign": signed, **extra}
    row["source_ref"] = certificate_source_ref(row)
    row["source_content_hash"] = certificate_content_hash(row)
    return row


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None
        self.rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.conn.calls.append((sql, params))
        state = self.conn.state
        if sql.startswith("SELECT GET_LOCK"):
            self.result = (self.conn.lock_result,)
        elif sql.startswith("SELECT RELEASE_LOCK"):
            self.conn.released = True
            self.result = (1,)
        elif sql.startswith("SELECT status,certificate_full_snapshot"):
            self.result = (state["status"], state["full"])
        elif sql.startswith("SELECT MAX(id)"):
            self.result = (state["latest"],)
        elif sql.startswith("SELECT id,source_ref,payload_json FROM registry_source_records"):
            self.rows = [(i + 1, row["source_ref"], json.dumps(row))
                         for i, row in enumerate(state["incoming"])]
        elif sql.startswith("UPDATE registry_import_issues SET status='superseded'"):
            for issue in state["issues"]:
                if issue["batch"] <= params[0] and issue["status"] == "pending":
                    issue["status"] = "superseded"
        elif sql.startswith("SELECT source_ref FROM registry_import_issues"):
            self.rows = [(issue["ref"],) for issue in state["issues"]
                         if issue["batch"] == params[0] and issue["status"] == "pending"]
        elif "FROM OnlineData._communities" in sql:
            self.result = (1, "测试社区")
        elif sql.startswith("SELECT id,community_id,normalized_address"):
            self.rows = [(7, 1, "合成测试路1号", self.conn.housing_type)]
        elif sql.startswith("SELECT id,source_ref,property_id"):
            self.rows = [(i, item["ref"], 7, item["hash"], item["payload"])
                         for i, item in state["certificates"].items()]
        elif sql.startswith("UPDATE registry_property_certificates SET source_missing_since"):
            for item in state["certificates"].values():
                item["missing"] = True
        elif sql.startswith("SELECT id,source_ref FROM registry_property_certificates"):
            self.rows = [(i, item["ref"]) for i, item in state["certificates"].items()
                         if item["ref"] in params]
        elif sql.startswith("SELECT COUNT(*) FROM registry_import_issues"):
            self.result = (sum(issue["batch"] == params[0] and issue["status"] == "pending"
                               for issue in state["issues"]),)
        elif sql.startswith("SELECT COUNT(*) FROM registry_source_records"):
            self.result = (len(state.get("links", [])),)
        elif sql.startswith("UPDATE registry_source_batches"):
            state["status"] = params[0]
            state["latest"] = params[-1]
        else:
            raise AssertionError(sql)

    async def executemany(self, sql, rows):
        self.conn.calls.append((sql, rows))
        state = self.conn.state
        if "INSERT INTO registry_import_issues" in sql:
            for values in rows:
                state["issues"].append({"batch": values[0], "status": "pending", "ref": values[2]})
        elif "INSERT INTO registry_property_certificates" in sql:
            if self.conn.fail_write:
                raise RuntimeError("synthetic write failure")
            for values in rows:
                key = max(state["certificates"], default=0) + 1
                state["certificates"][key] = {"ref": values[1], "hash": values[2],
                                               "payload": json.loads(values[14]), "missing": False}
        elif "UPDATE registry_property_certificates SET property_id" in sql:
            if self.conn.fail_write:
                raise RuntimeError("synthetic write failure")
            for values in rows:
                state["certificates"][values[-1]].update(
                    ref=values[1], hash=values[2], payload=json.loads(values[14]), missing=False,
                )
        elif "UPDATE registry_property_certificates SET source_row" in sql:
            for _, key in rows:
                state["certificates"][key]["missing"] = False
        elif "UPDATE registry_source_records SET entity_id" in sql:
            state["links"] = list(rows)
        else:
            raise AssertionError(sql)

    async def fetchone(self):
        return self.result

    async def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, incoming, *, full=1, latest=0, status="preview"):
        old = notice(source_id=999, signed="是")
        self.state = {"incoming": incoming, "full": full, "latest": latest, "status": status,
                      "certificates": {10: {"ref": old["source_ref"], "hash": old["source_content_hash"],
                                             "payload": old, "missing": False}},
                      "issues": [{"batch": 1, "status": "pending", "ref": "old-issue"}]}
        self.calls = []
        self.lock_result = 1
        self.fail_write = False
        self.housing_type = "个人出租"
        self.released = False
        self.committed = False

    def cursor(self):
        return Cursor(self)

    async def begin(self):
        self.before = copy.deepcopy(self.state)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.state = self.before


class CertificateApplyTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, conn, batch=2):
        with patch.object(service, "load_certificate_comparison", AsyncMock(return_value={})):
            return await service.apply_certificate_batch(conn, batch, 1)

    async def test_new_snapshot_replaces_old_current_notice_and_issues(self):
        conn = Connection([notice()])
        result = await self.apply(conn)
        self.assertTrue(conn.state["certificates"][10]["missing"])
        current = [row for row in conn.state["certificates"].values() if not row["missing"]]
        self.assertEqual(["否"], [row["payload"]["isSign"] for row in current])
        self.assertEqual("superseded", conn.state["issues"][0]["status"])
        self.assertEqual(0, result["pending_issue_count"])
        self.assertTrue(conn.committed and conn.released)

    async def test_changed_signature_on_same_ref_updates_not_duplicates(self):
        incoming = notice(source_id=999)
        conn = Connection([incoming])
        result = await self.apply(conn)
        self.assertEqual(1, result["updated_count"])
        self.assertEqual(1, len(conn.state["certificates"]))
        self.assertFalse(conn.state["certificates"][10]["missing"])

    async def test_identical_source_duplicates_have_one_current_notice(self):
        conn = Connection([notice(), notice(source_id=2)])
        result = await self.apply(conn)
        self.assertEqual(1, result["inserted_count"])
        self.assertEqual(0, result["pending_issue_count"])

    async def test_non_rental_source_does_not_link_to_historical_notice(self):
        conn = Connection([notice(source_id=999)])
        conn.housing_type = "自购房屋"
        result = await self.apply(conn)
        self.assertEqual(1, result["pending_issue_count"])
        self.assertEqual([], conn.state.get("links", []))
        self.assertTrue(conn.state["certificates"][10]["missing"])

    async def test_current_unresolvable_conflicts_replace_old_issues(self):
        conn = Connection([notice(), notice(source_id=2, signed="是")])
        result = await self.apply(conn)
        self.assertEqual(4, result["pending_issue_count"])
        self.assertFalse(any(not row["missing"] for row in conn.state["certificates"].values()))

    async def test_write_failure_rolls_back_notice_invalidation_and_issue_changes(self):
        conn = Connection([notice()])
        before = copy.deepcopy(conn.state)
        conn.fail_write = True
        with self.assertRaises(RuntimeError):
            await self.apply(conn)
        self.assertEqual(before, conn.state)
        self.assertTrue(conn.released)
        self.assertFalse(conn.committed)

    async def test_old_or_unproven_or_empty_snapshot_cannot_replace_current(self):
        for conn in (Connection([notice()], latest=3), Connection([notice()], full=0), Connection([])):
            before = copy.deepcopy(conn.state)
            with self.assertRaises(service.CertificateSnapshotConflict):
                await self.apply(conn)
            self.assertEqual(before, conn.state)
            self.assertTrue(conn.released)

    async def test_repeat_confirmation_does_not_resurrect_history(self):
        conn = Connection([notice()])
        await self.apply(conn)
        before = copy.deepcopy(conn.state)
        result = await self.apply(conn)
        self.assertTrue(result["idempotent"])
        self.assertEqual(before, conn.state)

    async def test_named_lock_timeout_stops_before_any_transaction(self):
        conn = Connection([notice()])
        conn.lock_result = 0
        with self.assertRaises(service.CertificateSnapshotConflict):
            await self.apply(conn)
        self.assertFalse(hasattr(conn, "before"))


if __name__ == "__main__":
    unittest.main()
