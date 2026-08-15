from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services import registry_certificate_jobs as jobs


class _Cursor:
    def __init__(self, state):
        self.state = state
        self.result = None
        self.results = []
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.rowcount = 0
        if normalized.startswith("UPDATE registry_certificate_source_runs SET status='running'"):
            run_id = int(params[0])
            run = self.state["runs"][run_id]
            if run["status"] == "pending":
                run.update(status="running", phase="reading")
                self.rowcount = 1
            return
        if normalized.startswith("SELECT page_no,row_count"):
            run_id = int(params[0])
            self.results = [self.state["pages"][key] for key in sorted(self.state["pages"]) if key[0] == run_id]
            return
        if normalized.startswith("UPDATE registry_certificate_source_runs SET current_page"):
            if len(params) == 5:
                page, fetched, accepted, rejected, run_id = params
            else:
                page, fetched, accepted, rejected, run_id = (*params,)
            self.state["runs"][int(run_id)].update(
                current_page=int(page),
                fetched_count=int(fetched),
                accepted_count=int(accepted),
                rejected_count=int(rejected),
            )
            self.rowcount = 1
            return
        if normalized.startswith("INSERT INTO registry_certificate_source_pages"):
            run_id, page_no, row_count, accepted, rejected, fingerprint, payload = params
            self.state["pages"][(int(run_id), int(page_no))] = (
                int(page_no), int(row_count), int(accepted), int(rejected), str(fingerprint), payload,
            )
            self.state.setdefault("inserted_pages", []).append((int(run_id), int(page_no)))
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE registry_certificate_source_runs SET phase="):
            phase = "classifying" if "phase='classifying'" in normalized else "writing_preview"
            self.state["runs"][int(params[0])]["phase"] = phase
            self.rowcount = 1
            return
        if normalized.startswith("SELECT requested_by"):
            self.result = (self.state["runs"][int(params[0])]["requested_by"],)
            return
        if normalized.startswith("DELETE FROM registry_certificate_source_pages"):
            run_id = int(params[0])
            self.state["pages"] = {
                key: value for key, value in self.state["pages"].items() if key[0] != run_id
            }
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE registry_certificate_source_runs SET status='completed'"):
            batch_id, summary, run_id = params
            self.state["runs"][int(run_id)].update(
                status="completed",
                phase="finished",
                batch_id=int(batch_id),
                summary=json.loads(summary),
            )
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE registry_certificate_source_runs SET status='failed'"):
            code, message, run_id = params
            self.state["runs"][int(run_id)].update(
                status="failed",
                phase="finished",
                error_code=str(code),
                error_message=str(message),
            )
            self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    async def fetchone(self):
        return self.result

    async def fetchall(self):
        return self.results


class _Connection:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _Cursor(self.state)


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    def __await__(self):
        async def resolve():
            return self.conn
        return resolve().__await__()

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, state):
        self.conn = _Connection(state)

    def acquire(self):
        return _Acquire(self.conn)

    def release(self, _conn):
        return None


def _page(number, raw_count, rows, rejected, fingerprint, is_last):
    return {
        "page": number,
        "raw_count": raw_count,
        "rows": rows,
        "rejected_count": rejected,
        "fingerprint": fingerprint,
        "is_last": is_last,
    }


class RegistryCertificateJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_run_persists_every_page_and_finishes_preview(self):
        state = {
            "runs": {1: {"status": "pending", "phase": "queued", "requested_by": 7}},
            "pages": {},
        }
        pool = _Pool(state)
        captured = {}

        async def pages(*, start_page=1):
            self.assertEqual(1, start_page)
            yield _page(1, 200, [{"address": "A", "community": "甲"}], 199, "a" * 64, False)
            yield _page(2, 1, [{"address": "B", "community": "乙"}], 0, "b" * 64, True)

        async def preview(_conn, rows, created_by):
            captured["rows"] = rows
            captured["created_by"] = created_by
            return {"batch_id": 91, "status": "preview", "total_count": 2, "normal_count": 2}

        with (
            patch.object(jobs.db_manager, "get_pool", return_value=pool),
            patch.object(jobs, "iter_certificate_pages", pages),
            patch.object(jobs, "_create_preview_batch", preview),
        ):
            await jobs.run_certificate_source_run(1)

        self.assertEqual("completed", state["runs"][1]["status"])
        self.assertEqual(201, state["runs"][1]["fetched_count"])
        self.assertEqual(2, state["runs"][1]["accepted_count"])
        self.assertEqual(199, state["runs"][1]["rejected_count"])
        self.assertEqual([(1, 1), (1, 2)], state["inserted_pages"])
        self.assertEqual({}, state["pages"])
        self.assertEqual([1, 2], [row["source_row"] for row in captured["rows"]])
        self.assertEqual(7, captured["created_by"])

    async def test_resume_rechecks_last_saved_page_before_continuing(self):
        saved_payload = json.dumps([{"address": "A", "community": "甲"}], ensure_ascii=False)
        state = {
            "runs": {2: {"status": "pending", "phase": "queued", "requested_by": 8}},
            "pages": {(2, 1): (1, 200, 1, 199, "a" * 64, saved_payload)},
        }
        pool = _Pool(state)

        async def pages(*, start_page=1):
            self.assertEqual(1, start_page)
            yield _page(1, 200, [{"address": "A", "community": "甲"}], 199, "a" * 64, False)
            yield _page(2, 1, [{"address": "B", "community": "乙"}], 0, "b" * 64, True)

        async def preview(_conn, rows, _created_by):
            return {"batch_id": 92, "status": "preview", "total_count": len(rows)}

        with (
            patch.object(jobs.db_manager, "get_pool", return_value=pool),
            patch.object(jobs, "iter_certificate_pages", pages),
            patch.object(jobs, "_create_preview_batch", preview),
        ):
            await jobs.run_certificate_source_run(2)

        self.assertEqual("completed", state["runs"][2]["status"])
        self.assertEqual([(2, 2)], state["inserted_pages"])
        self.assertEqual({}, state["pages"])
        self.assertEqual(201, state["runs"][2]["fetched_count"])

    async def test_resume_stops_when_last_saved_page_has_changed(self):
        saved_payload = json.dumps([{"address": "A", "community": "甲"}], ensure_ascii=False)
        state = {
            "runs": {3: {"status": "pending", "phase": "queued", "requested_by": 9}},
            "pages": {(3, 1): (1, 200, 1, 199, "a" * 64, saved_payload)},
        }
        pool = _Pool(state)

        async def pages(*, start_page=1):
            self.assertEqual(1, start_page)
            yield _page(1, 200, [{"address": "已变化", "community": "甲"}], 199, "c" * 64, False)

        with (
            patch.object(jobs.db_manager, "get_pool", return_value=pool),
            patch.object(jobs, "iter_certificate_pages", pages),
        ):
            await jobs.run_certificate_source_run(3)

        self.assertEqual("failed", state["runs"][3]["status"])
        self.assertEqual("source_changed", state["runs"][3]["error_code"])
        self.assertIn((3, 1), state["pages"])


if __name__ == "__main__":
    unittest.main()
