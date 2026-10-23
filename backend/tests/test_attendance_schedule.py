"""Behavior tests using SQLite as a SQL adapter, not real-MySQL validation."""

import os
import re
import sqlite3
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")
from fastapi import HTTPException
from services import attendance_schedule as service
from services.personnel_attendance import allocate_person_days
from migrations.personnel_attendance import converted_rows, measure, verify
import json
from routers.personnel_attendance import WeekendAssignment, attendance_view


class Cursor:
    def __init__(self, conn):
        self.raw = conn.raw.cursor()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.raw.close()

    async def execute(self, sql, params=()):
        if sql == "START TRANSACTION":
            sql = "BEGIN"
        if sql.startswith("SET TRANSACTION"):
            return
        sql = (
            re.sub(r"`[^`]+`\.`([^`]+)`", r"\1", sql)
            .replace(" FOR UPDATE", "")
            .replace("%s", "?")
        )
        sql = sql.replace("INSERT IGNORE", "INSERT OR IGNORE").replace(
            "UTC_TIMESTAMP()", "CURRENT_TIMESTAMP"
        )
        if "ON DUPLICATE KEY UPDATE" in sql:
            sql = sql.replace(
                "ON DUPLICATE KEY UPDATE",
                "ON CONFLICT(member_id,work_date) DO UPDATE SET",
            )
            sql = re.sub(r"VALUES\((\w+)\)", r"excluded.\1", sql)
        self.raw.execute(sql, params or ())
        self.lastrowid = self.raw.lastrowid

    async def fetchone(self):
        return self.raw.fetchone()

    async def fetchall(self):
        return self.raw.fetchall()


class Connection:
    def __init__(self):
        self.raw = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.raw.executescript("""
          CREATE TABLE _system_config(config_key TEXT PRIMARY KEY,config_value TEXT);
          INSERT INTO _system_config VALUES('attendance_daily_schedule_v1','1');
          CREATE TABLE _grid_members(id INTEGER PRIMARY KEY,name TEXT,position TEXT,attendance_revision INT DEFAULT 0,
            status TEXT DEFAULT '在岗',notes TEXT,department_id INT,area_id INT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,leave_start_date DATE,leave_end_date DATE,leave_reason TEXT,leave_source TEXT);
          INSERT INTO _grid_members(id,name,position) VALUES(1,'合成人员甲','组员'),(2,'合成人员乙','组员');
          CREATE TABLE _personnel_work_schedule(member_id INT,work_date DATE,state TEXT,source TEXT,revision INT,updated_by INT,
            updated_at TEXT,PRIMARY KEY(member_id,work_date));
          CREATE TABLE _personnel_attendance_history(id INTEGER PRIMARY KEY AUTOINCREMENT,member_id INT,absence_type TEXT,
            start_date DATE,end_date DATE,reason TEXT,source TEXT,created_by INT,is_active INT DEFAULT 1,
            workflow_ticket_id INT,superseded_by INT,correction_action TEXT DEFAULT '',correction_reason TEXT,
            corrected_by INT,corrected_at TEXT);
          CREATE TABLE _personnel_attendance_changes(id INTEGER PRIMARY KEY,member_id INT,revision INT,action TEXT,
            actor_id INT,reason TEXT,before_json TEXT,after_json TEXT,UNIQUE(member_id,revision));
          CREATE TABLE _personnel_attendance_operations(request_id TEXT PRIMARY KEY,actor_id INT,payload_hash TEXT,response_json TEXT);
        """)

    def cursor(self):
        return Cursor(self)

    async def begin(self):
        self.raw.execute("BEGIN")

    async def commit(self):
        self.raw.commit()

    async def rollback(self):
        self.raw.rollback()


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = Connection()
        for name, result in [
            ("get_business_date", date(2026, 10, 19)),
            ("get_configured_positions", ["组员"]),
        ]:
            patcher = patch.object(service, name, AsyncMock(return_value=result))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.conn.raw.close)

    def schedule(self, **kw):
        data = dict(
            request_id="request-0000000001",
            member_ids=[1],
            expected_versions={1: 0},
            start_date=date(2026, 10, 24),
            end_date=date(2026, 10, 25),
            state="work",
            reason="合成安排",
        )
        data.update(kw)
        return data

    def leave(self, **kw):
        data = dict(
            request_id="leave-000000000001",
            member_id=1,
            expected_version=0,
            action="create",
            record_id=None,
            start_date=date(2026, 10, 19),
            end_date=date(2026, 10, 21),
            absence_type="temporary_leave",
            reason="合成登记",
        )
        data.update(kw)
        return data

    def weekly(self, **kw):
        data = dict(
            request_id="weekly-000000000001",
            week_start=date(2026, 10, 19),
            expected_versions={1: 0},
            assignments=[
                {
                    "member_id": 1,
                    "days": [
                        {"date": date(2026, 10, 19), "state": "rest"},
                        {"date": date(2026, 10, 24), "state": "work"},
                    ],
                }
            ],
        )
        data.update(kw)
        return data

    async def test_preview_rolls_back_and_double_duty_counts_two_person_days(self):
        data = self.schedule()
        preview = await service.schedule_mutation(self.conn, data, 9, True)
        self.assertEqual(len(preview["changes"]), 2)
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT COUNT(*) FROM _personnel_work_schedule"
            ).fetchone()[0],
            0,
        )
        await service.schedule_mutation(self.conn, data, 9)
        async with self.conn.cursor() as cur:
            daily = await service.load_daily(
                cur, [1], data["start_date"], data["end_date"]
            )
        member = dict(id=1, name="合成人员甲", community="合成社区", position="组员")
        context = dict(
            members={member["name"]: member},
            periods={},
            daily=daily,
            weekend_duty_positions={"组员"},
            missing_week_starts=set(),
            history_started_on=date(2026, 1, 1),
            legacy_history_incomplete=False,
        )
        result = allocate_person_days(
            start_date=data["start_date"],
            end_date=data["end_date"],
            daily_visits={},
            context=context,
            include_unknown=False,
        )
        self.assertEqual(result["total_person_days"], 2)

    async def test_retries_are_idempotent_and_stale_versions_conflict(self):
        data = self.schedule()
        first = await service.schedule_mutation(self.conn, data, 9)
        self.assertEqual(await service.schedule_mutation(self.conn, data, 9), first)
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT COUNT(*) FROM _personnel_attendance_changes"
            ).fetchone()[0],
            1,
        )
        data["request_id"] = "request-0000000002"
        with self.assertRaises(HTTPException) as caught:
            await service.schedule_mutation(self.conn, data, 9)
        self.assertEqual(caught.exception.status_code, 409)

    async def test_weekly_preview_rolls_back_and_commit_retry_is_idempotent(self):
        data = self.weekly()
        preview = await service.weekly_schedule_mutation(self.conn, data, 9, True)
        self.assertEqual(len(preview["changes"]), 2)
        self.assertEqual(
            self.conn.raw.execute("SELECT COUNT(*) FROM _personnel_work_schedule").fetchone()[0],
            0,
        )

        committed = await service.weekly_schedule_mutation(self.conn, data, 9)
        retried = await service.weekly_schedule_mutation(self.conn, data, 9)
        self.assertEqual(retried, committed)
        self.assertEqual(
            self.conn.raw.execute("SELECT COUNT(*) FROM _personnel_work_schedule").fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.raw.execute("SELECT COUNT(*) FROM _personnel_attendance_changes").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.raw.execute("SELECT attendance_revision FROM _grid_members WHERE id=1").fetchone()[0],
            1,
        )

    async def test_weekly_schedule_skips_leave_and_restores_defaults(self):
        await service.leave_mutation(
            self.conn,
            self.leave(end_date=date(2026, 10, 19)),
            9,
        )
        data = self.weekly(
            request_id="weekly-000000000002",
            expected_versions={1: 1},
            assignments=[
                {
                    "member_id": 1,
                    "days": [
                        {"date": date(2026, 10, 19), "state": "rest"},
                        {"date": date(2026, 10, 20), "state": "rest"},
                    ],
                }
            ],
        )
        result = await service.weekly_schedule_mutation(self.conn, data, 9, True)
        self.assertEqual(result["skipped"], [{"member_id": 1, "date": "2026-10-19"}])
        self.assertEqual(len(result["changes"]), 1)
        await service.weekly_schedule_mutation(self.conn, data, 9)
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT state FROM _personnel_work_schedule WHERE member_id=1 AND work_date=?",
                (date(2026, 10, 20),),
            ).fetchone()[0],
            "rest",
        )

        self.conn.raw.execute(
            "INSERT INTO _personnel_work_schedule(member_id,work_date,state,source,revision,updated_by) "
            "VALUES(1,?,'rest','manual',2,9),(1,?,'work','manual',2,9)",
            (date(2026, 10, 21), date(2026, 10, 24)),
        )
        self.conn.raw.commit()
        reset = self.weekly(
            request_id="weekly-000000000003",
            expected_versions={1: 2},
            assignments=[
                {
                    "member_id": 1,
                    "days": [
                        {"date": date(2026, 10, 21), "state": None},
                        {"date": date(2026, 10, 24), "state": None},
                    ],
                }
            ],
        )
        restored = await service.weekly_schedule_mutation(self.conn, reset, 9)
        self.assertEqual([item["after"] for item in restored["changes"]], ["work", "unassigned"])
        self.assertIsNone(
            self.conn.raw.execute(
                "SELECT state FROM _personnel_work_schedule WHERE member_id=1 AND work_date=?",
                (date(2026, 10, 21),),
            ).fetchone()
        )
        self.assertIsNone(
            self.conn.raw.execute(
                "SELECT state FROM _personnel_work_schedule WHERE member_id=1 AND work_date=?",
                (date(2026, 10, 24),),
            ).fetchone()
        )

    async def test_weekly_schedule_rejects_stale_versions_and_rolls_back_batch(self):
        stale = self.weekly(expected_versions={1: 1})
        with self.assertRaises(HTTPException) as caught:
            await service.weekly_schedule_mutation(self.conn, stale, 9)
        self.assertEqual(caught.exception.status_code, 409)

        data = self.weekly(
            request_id="weekly-000000000004",
            expected_versions={1: 0, 2: 0},
            assignments=[
                {"member_id": member_id, "days": [{"date": date(2026, 10, 24), "state": "work"}]}
                for member_id in (1, 2)
            ],
        )
        original_record_change = service.record_change
        calls = 0

        async def fail_on_second_member(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected weekly audit failure")
            await original_record_change(*args)

        with patch.object(service, "record_change", side_effect=fail_on_second_member):
            with self.assertRaisesRegex(RuntimeError, "weekly audit failure"):
                await service.weekly_schedule_mutation(self.conn, data, 9)
        self.assertEqual(
            self.conn.raw.execute("SELECT COUNT(*) FROM _personnel_work_schedule").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.raw.execute("SELECT COUNT(*) FROM _personnel_attendance_changes").fetchone()[0],
            0,
        )

    async def test_request_id_cannot_be_reused_by_other_actor_or_payload(self):
        data = self.schedule()
        await service.schedule_mutation(self.conn, data, 9)
        with self.assertRaises(HTTPException):
            await service.schedule_mutation(self.conn, data, 10)
        data["state"] = "rest"
        with self.assertRaises(HTTPException):
            await service.schedule_mutation(self.conn, data, 9)

    async def test_weekday_move_leaves_original_days_rest_and_overlap_work(self):
        data = self.schedule(
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 22),
            original_start=date(2026, 10, 19),
            original_end=date(2026, 10, 21),
        )
        await service.schedule_mutation(self.conn, data, 9)
        rows = self.conn.raw.execute(
            "SELECT work_date,state FROM _personnel_work_schedule ORDER BY work_date"
        ).fetchall()
        self.assertEqual(
            rows, [(date(2026, 10, 19), "rest"), (date(2026, 10, 22), "work")]
        )

    async def test_leave_conflict_and_batch_failure_leave_no_partial_writes(self):
        await service.leave_mutation(self.conn, self.leave(), 9)
        data = self.schedule(
            member_ids=[1, 2],
            expected_versions={1: 1, 2: 0},
            start_date=date(2026, 10, 19),
            end_date=date(2026, 10, 20),
        )
        with self.assertRaises(HTTPException):
            await service.schedule_mutation(self.conn, data, 9)
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT COUNT(*) FROM _personnel_work_schedule"
            ).fetchone()[0],
            0,
        )
        with patch.object(
            service,
            "record_change",
            AsyncMock(side_effect=RuntimeError("injected failure")),
        ):
            with self.assertRaises(RuntimeError):
                await service.schedule_mutation(
                    self.conn, self.schedule(expected_versions={1: 1}), 9
                )
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT COUNT(*) FROM _personnel_work_schedule"
            ).fetchone()[0],
            0,
        )

    async def test_correction_revoke_keep_original_and_workflow_link(self):
        created = await service.leave_mutation(self.conn, self.leave(), 9)
        record = created["record_id"]
        self.conn.raw.execute(
            "UPDATE _personnel_attendance_history SET source='workflow',workflow_ticket_id=7 WHERE id=?",
            (record,),
        )
        self.conn.raw.commit()
        corrected = self.leave(
            request_id="leave-000000000002",
            expected_version=1,
            action="correct",
            record_id=record,
            start_date=date(2026, 10, 20),
        )
        new = await service.leave_mutation(self.conn, corrected, 9)
        rows = self.conn.raw.execute(
            "SELECT start_date,is_active,superseded_by,workflow_ticket_id FROM _personnel_attendance_history ORDER BY id"
        ).fetchall()
        self.assertEqual(rows[0], (date(2026, 10, 19), 0, new["record_id"], 7))
        self.assertEqual(rows[1][3], 7)
        revoked = self.leave(
            request_id="leave-000000000003",
            expected_version=2,
            action="revoke",
            record_id=new["record_id"],
        )
        await service.leave_mutation(self.conn, revoked, 9)
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT SUM(is_active) FROM _personnel_attendance_history"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(
            self.conn.raw.execute(
                "SELECT leave_start_date FROM _grid_members WHERE id=1"
            ).fetchone()[0]
        )
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT COUNT(*) FROM _personnel_attendance_changes"
            ).fetchone()[0],
            3,
        )

    async def test_overlap_rejected_and_long_term_resume_has_explicit_end(self):
        created = await service.leave_mutation(
            self.conn, self.leave(absence_type="long_term_leave", end_date=None), 9
        )
        with self.assertRaises(HTTPException):
            await service.leave_mutation(
                self.conn,
                self.leave(request_id="leave-000000000002", expected_version=1),
                9,
            )
        resumed = self.leave(
            request_id="leave-000000000003",
            expected_version=1,
            action="resume",
            record_id=created["record_id"],
            start_date=date(2026, 10, 23),
        )
        await service.leave_mutation(self.conn, resumed, 9)
        row = self.conn.raw.execute(
            "SELECT end_date FROM _personnel_attendance_history WHERE is_active=1"
        ).fetchone()
        self.assertEqual(row[0], date(2026, 10, 22))

    async def test_restart_seed_does_not_resurrect_revoked_legacy_state(self):
        self.conn.raw.execute(
            "INSERT INTO _system_config VALUES('attendance_legacy_seed_v1','1')"
        )
        self.conn.raw.commit()
        created = await service.leave_mutation(self.conn, self.leave(), 9)
        await service.leave_mutation(
            self.conn,
            self.leave(
                request_id="leave-000000000002",
                expected_version=1,
                action="revoke",
                record_id=created["record_id"],
            ),
            9,
        )
        # Even a stale compatibility mirror cannot trigger another bootstrap seed.
        self.conn.raw.execute(
            "UPDATE _grid_members SET leave_start_date='2026-10-19',leave_end_date='2026-10-21' WHERE id=1"
        )
        self.conn.raw.commit()
        async with self.conn.cursor() as cur:
            await service.seed_legacy_once(cur)
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT SUM(is_active) FROM _personnel_attendance_history"
            ).fetchone()[0],
            0,
        )

    async def test_first_seed_commits_marker_with_history_and_runs_once(self):
        self.conn.raw.execute(
            "UPDATE _grid_members SET leave_start_date='2026-10-19',leave_end_date='2026-10-21' WHERE id=1"
        )
        self.conn.raw.commit()
        async with self.conn.cursor() as cur:
            await service.seed_legacy_once(cur)
            await service.seed_legacy_once(cur)
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT COUNT(*) FROM _personnel_attendance_history"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT config_value FROM _system_config WHERE config_key='attendance_legacy_seed_v1'"
            ).fetchone()[0],
            "1",
        )

    async def test_migration_verify_requires_original_evidence_and_audits_later_edits(
        self,
    ):
        self.conn.raw.executescript(
            "CREATE TABLE _personnel_weekend_duty(member_id INT,week_start DATE,duty_date DATE,updated_by INT);"
        )
        self.conn.raw.execute(
            "INSERT INTO _personnel_weekend_duty VALUES(1,'2026-10-19','2026-10-24',9)"
        )
        self.conn.raw.execute(
            "INSERT INTO _personnel_work_schedule(member_id,work_date,state,source,revision) VALUES(1,'2026-10-24','work','legacy_weekend',0),(1,'2026-10-25','rest','legacy_weekend',0)"
        )
        self.conn.raw.commit()
        async with self.conn.cursor() as cur:
            self.assertFalse((await verify(cur))["verified"])
            report, _ = await measure(cur)
            await cur.execute(
                "INSERT INTO _system_config VALUES('attendance_migration_evidence',%s)",
                (json.dumps(report),),
            )
        self.conn.raw.commit()
        async with self.conn.cursor() as cur:
            self.assertTrue((await verify(cur))["verified"])
        # A legitimate later schedule change must not invalidate the baseline.
        await service.schedule_mutation(self.conn, self.schedule(state="rest"), 9)
        async with self.conn.cursor() as cur:
            result = await verify(cur)
            self.assertTrue(result["verified"])
            self.assertEqual(result["edited_since_migration"], 1)
        # A raw source replacement without its immutable change fails verification.
        self.conn.raw.execute(
            "UPDATE _personnel_work_schedule SET state='work' WHERE work_date='2026-10-24'"
        )
        self.conn.raw.commit()
        async with self.conn.cursor() as cur:
            self.assertFalse((await verify(cur))["verified"])
        # Restoring the full migration row used to evade the source-specific
        # branch even though the later immutable edit still says "rest".
        self.conn.raw.execute(
            "UPDATE _personnel_work_schedule SET state='work',source='legacy_weekend',revision=0 WHERE work_date='2026-10-24'"
        )
        self.conn.raw.commit()
        async with self.conn.cursor() as cur:
            self.assertFalse((await verify(cur))["verified"])
        await service.schedule_mutation(
            self.conn,
            self.schedule(state="rest", expected_versions={1: 1}, request_id="request-latest-edit"),
            9,
        )
        async with self.conn.cursor() as cur:
            self.assertTrue((await verify(cur))["verified"])
        # Matching an older valid audit is insufficient when a newer revision
        # exists for this date, even if both changes ended in the same state.
        self.conn.raw.execute(
            "UPDATE _personnel_work_schedule SET source='manual',revision=1 WHERE work_date='2026-10-24'"
        )
        self.conn.raw.commit()
        async with self.conn.cursor() as cur:
            self.assertFalse((await verify(cur))["verified"])
        self.conn.raw.execute(
            "DELETE FROM _personnel_work_schedule WHERE work_date='2026-10-25'"
        )
        self.conn.raw.commit()
        async with self.conn.cursor() as cur:
            self.assertEqual((await verify(cur))["missing"], 1)

    async def test_workflow_cancellation_revokes_latest_correction_preserving_approval(
        self,
    ):
        from routers.workflow import (
            _apply_leave_attendance,
            _deactivate_leave_attendance,
        )

        self.conn.raw.executescript(
            "CREATE TABLE leave_request_details(work_order_id INT,member_id INT,leave_type TEXT,start_date DATE,end_date DATE,reason TEXT,attendance_record_id INT);"
        )
        self.conn.raw.execute(
            "INSERT INTO leave_request_details VALUES(7,1,'sick_leave','2026-10-19','2026-10-21','合成原因',NULL)"
        )
        self.conn.raw.commit()
        async with self.conn.cursor() as cur:
            original = await _apply_leave_attendance(cur, 7, 9)
        self.conn.raw.commit()
        await service.leave_mutation(
            self.conn,
            self.leave(
                action="correct",
                record_id=original,
                expected_version=1,
                start_date=date(2026, 10, 20),
            ),
            9,
        )
        async with self.conn.cursor() as cur:
            await _deactivate_leave_attendance(cur, 7, 9)
        self.conn.raw.commit()
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT attendance_record_id,start_date FROM leave_request_details"
            ).fetchone(),
            (original, date(2026, 10, 19)),
        )
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT SUM(is_active) FROM _personnel_attendance_history"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT attendance_revision FROM _grid_members WHERE id=1"
            ).fetchone()[0],
            3,
        )

    async def test_calendar_options_does_not_require_basic_view_or_expose_sensitive_fields(
        self,
    ):
        from routers.personnel_attendance import calendar_options

        self.conn.raw.executescript(
            "CREATE TABLE _departments(id INT,name TEXT,parent_id INT,is_active INT); CREATE TABLE _grid_member_department_links(member_id INT,department_id INT); INSERT INTO _departments VALUES(1,'合成部门',NULL,1),(2,'合成子部门',1,1); INSERT INTO _grid_member_department_links VALUES(1,2);"
        )
        result = await calendar_options(
            keyword="合成",
            department_id=1,
            user={"permissions": ["attendance.manage"]},
            conn=self.conn,
        )
        self.assertEqual([m["id"] for m in result["members"]], [1])
        self.assertEqual(
            set(result["members"][0]), {"id", "name", "position", "attendance_revision"}
        )

    async def test_worklog_roster_uses_individual_day_not_stale_status_or_whole_week(
        self,
    ):
        from services.work_log_daily_detail import _load_roster

        class RosterCursor:
            async def __aenter__(inner):
                return inner

            async def __aexit__(inner, *_):
                pass

            async def execute(inner, sql, *_):
                self.assertNotIn("member.status <>", sql)

            async def fetchall(inner):
                return [
                    (
                        1,
                        "合成人员甲",
                        "组员",
                        "离岗",
                        1,
                        "合成社区",
                        "",
                        "合成部门",
                        None,
                        "",
                    ),
                    (
                        2,
                        "合成人员乙",
                        "组员",
                        "在岗",
                        1,
                        "合成社区",
                        "",
                        "合成部门",
                        None,
                        "",
                    ),
                ]

        class RosterConnection:
            def cursor(inner):
                return RosterCursor()

        context = dict(
            members={},
            periods={},
            daily={(1, date(2026, 10, 24)): "work"},
            weekend_duty_positions={"组员"},
            missing_week_starts={date(2026, 10, 19)},
        )
        with patch(
            "services.work_log_daily_detail.get_attendance_context",
            AsyncMock(return_value=context),
        ), patch(
            "services.work_log_daily_detail._area_labels",
            AsyncMock(return_value=({}, {})),
        ):
            roster = await _load_roster(RosterConnection(), date(2026, 10, 24))
        self.assertEqual(
            {m["id"]: m["attendance"] for m in roster}, {1: "是", 2: "未排班"}
        )

    async def test_profile_edit_of_longterm_leave_does_not_validate_legacy_temporary_range(
        self,
    ):
        from routers.grid_members import GridMemberUpdate, update_member

        await service.leave_mutation(
            self.conn, self.leave(absence_type="long_term_leave", end_date=None), 9
        )
        with patch(
            "routers.grid_members.get_member_departments",
            AsyncMock(return_value={1: [{"id": 1}]}),
        ), patch("routers.grid_members.record_admin_audit", AsyncMock()), patch(
            "routers.grid_members.request_audit_fields", return_value={}
        ):
            await update_member(
                1,
                GridMemberUpdate(notes="合成备注"),
                request=None,
                user={"id": 9, "role": "admin"},
                conn=self.conn,
            )
        row = self.conn.raw.execute(
            "SELECT notes,status,attendance_revision FROM _grid_members WHERE id=1"
        ).fetchone()
        self.assertEqual(row, ("合成备注", "离岗", 1))

    async def test_initial_seed_also_preserves_preexisting_inactive_history(self):
        self.conn.raw.execute(
            "INSERT INTO _personnel_attendance_history(member_id,absence_type,start_date,end_date,source,is_active) VALUES(1,'temporary_leave','2026-10-19','2026-10-21','manual',0)"
        )
        self.conn.raw.execute(
            "UPDATE _grid_members SET leave_start_date='2026-10-19',leave_end_date='2026-10-21' WHERE id=1"
        )
        self.conn.raw.commit()
        async with self.conn.cursor() as cur:
            await service.seed_legacy_once(cur)
        self.assertEqual(
            self.conn.raw.execute(
                "SELECT COUNT(*),SUM(is_active) FROM _personnel_attendance_history"
            ).fetchone(),
            (1, 0),
        )

    async def test_sick_leave_remains_typed_and_linked_during_correction(self):
        async with self.conn.cursor() as cur:
            record = await service.insert_leave(
                cur,
                1,
                "sick_leave",
                date(2026, 10, 19),
                date(2026, 10, 20),
                "合成原因",
                "workflow",
                9,
                7,
            )
        self.conn.raw.commit()
        data = self.leave(
            action="correct",
            record_id=record,
            request_id="leave-000000000002",
            start_date=date(2026, 10, 20),
        )
        await service.leave_mutation(self.conn, data, 9)
        row = self.conn.raw.execute(
            "SELECT absence_type,workflow_ticket_id FROM _personnel_attendance_history WHERE is_active=1"
        ).fetchone()
        self.assertEqual(row, ("sick_leave", 7))

    async def test_unmigrated_write_is_blocked(self):
        self.conn.raw.execute("UPDATE _system_config SET config_value='0'")
        self.conn.raw.commit()
        with self.assertRaises(HTTPException):
            await service.schedule_mutation(self.conn, self.schedule(), 9)

    async def test_permissions_and_old_client_contract(self):
        with self.assertRaises(HTTPException):
            await attendance_view({"permissions": ["personnel.basic.view"]})
        self.assertIsNotNone(
            await attendance_view({"permissions": ["attendance.manage"]})
        )
        with self.assertRaises(ValueError):
            WeekendAssignment(member_id=1, duty_day="saturday")
        WeekendAssignment(
            member_id=1, duty_days=["saturday", "sunday"], expected_version=0
        )

    def test_migration_maps_rest_and_both_dates_without_inventing_missing_people(self):
        week = date(2026, 10, 19)
        rows = converted_rows([(1, week, date(2026, 10, 24), 9), (2, week, None, 9)])
        self.assertEqual(
            [(m, s) for m, _, s, _ in rows],
            [(1, "work"), (1, "rest"), (2, "rest"), (2, "rest")],
        )

    def test_resolver_preserves_conflict_and_unknown_weekend(self):
        m, day = dict(id=1, position="组员"), date(2026, 10, 24)
        self.assertEqual(
            service.resolve_day(m, day, {}, [], {"组员"})["state"], "unassigned"
        )
        self.assertEqual(
            service.resolve_day(m, day, {(1, day): "rest"}, [], {"组员"})["state"],
            "rest",
        )
        leave = dict(id=1, start_date=day, end_date=day, absence_type="temporary_leave")
        result = service.resolve_day(m, day, {(1, day): "work"}, [leave], {"组员"})
        self.assertEqual(result["state"], "leave")
        self.assertTrue(result["conflict"])
