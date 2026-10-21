import type { AttendanceDay, AttendanceState } from "../api/client.ts";

export const ATTENDANCE_LABELS: Record<AttendanceState, string> = {
  work: "上班",
  rest: "休息",
  leave: "请假",
  long_term_leave: "长期离岗",
  unassigned: "未排班",
};
export interface AttendanceSpan {
  memberId: number;
  state: AttendanceState;
  start: string;
  end: string;
  leaveId: number | null;
  conflict: boolean;
  overlapping: boolean;
}
export function addDays(date: string, amount: number): string {
  const value = new Date(`${date}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() + amount);
  return value.toISOString().slice(0, 10);
}
export function daysBetween(start: string, end: string): number {
  return Math.round(
    (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) /
      86400000,
  );
}
export function attendanceSpans(days: AttendanceDay[]): AttendanceSpan[] {
  const sorted = [...days].sort(
    (a, b) => a.member_id - b.member_id || a.date.localeCompare(b.date),
  );
  const spans: AttendanceSpan[] = [];
  for (const day of sorted) {
    const last = spans[spans.length - 1];
    if (
      last &&
      last.memberId === day.member_id &&
      last.state === day.state &&
      last.leaveId === day.leave_id &&
      last.conflict === day.conflict &&
      last.overlapping === day.overlapping &&
      addDays(last.end, 1) === day.date
    ) {
      last.end = day.date;
    } else
      spans.push({
        memberId: day.member_id,
        state: day.state,
        start: day.date,
        end: day.date,
        leaveId: day.leave_id,
        conflict: day.conflict,
        overlapping: day.overlapping,
      });
  }
  return spans;
}
export function weekSegments(spans: AttendanceSpan[], weekStart: string) {
  const weekEnd = addDays(weekStart, 6);
  return spans
    .filter((s) => s.start <= weekEnd && s.end >= weekStart)
    .map((span) => {
      const start = span.start < weekStart ? weekStart : span.start;
      const end = span.end > weekEnd ? weekEnd : span.end;
      return {
        span,
        start,
        end,
        column: daysBetween(weekStart, start) + 1,
        length: daysBetween(start, end) + 1,
        continuesBefore: span.start < weekStart,
        continuesAfter: span.end > weekEnd,
      };
    });
}
