import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  addDays,
  attendanceSpans,
  weekSegments,
} from "../src/utils/attendanceCalendar.ts";
const day = (
  date: string,
  state: "work" | "leave" = "work",
  id = 1,
  leave_id: number | null = null,
) => ({
  member_id: id,
  date,
  state,
  leave_id,
  source: "schedule",
  conflict: false,
  overlapping: false,
});
test("跨月、闰年按业务日期处理，不受本地夏令时影响", () => {
  assert.equal(addDays("2028-02-28", 2), "2028-03-01");
  assert.equal(addDays("2026-12-31", 1), "2027-01-01");
});
test("仅连续同人员同状态同请假来源的日期合并", () => {
  const spans = attendanceSpans([
    day("2026-10-20"),
    day("2026-10-19"),
    day("2026-10-21", "leave", 1, 4),
    day("2026-10-22", "leave", 1, 5),
    day("2026-10-20", "work", 2),
  ]);
  assert.equal(spans.length, 4);
  assert.deepEqual(
    [spans[0].start, spans[0].end],
    ["2026-10-19", "2026-10-20"],
  );
});
test("跨周横条保持同一逻辑区间并显示延续标识", () => {
  const span = {
    memberId: 1,
    state: "work" as const,
    start: "2026-10-16",
    end: "2026-10-28",
    leaveId: null,
    conflict: false,
    overlapping: false,
  };
  const parts = weekSegments([span], "2026-10-19");
  assert.equal(parts[0].column, 1);
  assert.equal(parts[0].length, 7);
  assert.equal(parts[0].continuesBefore, true);
  assert.equal(parts[0].continuesAfter, true);
  assert.equal(parts[0].span.start, "2026-10-16");
});

test("周排班确认期间冻结草稿并在提交重试时复用请求编号", () => {
  const page = readFileSync(new URL("../src/pages/WeekendDuty.tsx", import.meta.url), "utf8");
  const client = readFileSync(new URL("../src/api/client.ts", import.meta.url), "utf8");

  assert.match(page, /const requestId = crypto\.randomUUID\(\);/);
  assert.match(page, /changeWeeklySchedule\(board\.week_start, assignments, false, requestId\)/);
  assert.match(page, /const editingLocked = busy \|\| confirmOpen;/);
  assert.match(page, /disabled=\{!editable \|\| editingLocked \|\| leave\}/);
  assert.match(page, /onCancel: \(\) => setConfirmOpen\(false\)/);
  assert.match(client, /requestId = crypto\.randomUUID\(\)/);
  assert.match(client, /request_id: requestId, assignments/);
});

test("系统建议在岗使用只读接口并明确区分正式排班", () => {
  const page = readFileSync(new URL("../src/pages/AttendanceCalendar.tsx", import.meta.url), "utf8");
  const client = readFileSync(new URL("../src/api/client.ts", import.meta.url), "utf8");

  assert.match(client, /getAttendanceSuggestions/);
  assert.match(client, /\/personnel\/attendance\/suggestions/);
  assert.match(page, /系统建议在岗/);
  assert.match(page, /suggestion_state/);
  assert.match(page, /不会修改正式排班/);
});
