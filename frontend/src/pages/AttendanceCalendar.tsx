import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
  Drawer,
  Empty,
  Input,
  Modal,
  Pagination,
  Select,
  Spin,
  Tag,
} from "antd";
import dayjs from "dayjs";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  changeAttendance,
  getAttendanceCalendar,
  getAttendanceHistory,
  getAttendanceOptions,
  formatUTCTime,
  type AttendanceCalendarData,
  type AttendanceHistoryItem,
  type AttendancePreview,
  type LeaveChange,
  type ScheduleChange,
  type CalendarLeave,
} from "../api/client";
import { PageHeader, ListToolbar, Panel } from "../components/ui";
import { useAuth } from "../context/AuthContext";
import { useResponsiveLayout } from "../hooks/useResponsiveLayout";
import {
  addDays,
  attendanceSpans,
  ATTENDANCE_LABELS,
  daysBetween,
  weekSegments,
  type AttendanceSpan,
} from "../utils/attendanceCalendar";
import "./AttendanceCalendar.css";

type Editor = {
  kind: "schedule" | "leave";
  members: number[];
  start: string;
  end: string;
  state: "work" | "rest";
  action: "create" | "correct" | "revoke" | "resume";
  absenceType: "temporary_leave" | "long_term_leave";
  recordId?: number;
  originalStart?: string;
  originalEnd?: string;
  reason: string;
  versions: Record<number, number>;
  leaveReason: string;
};
const errorText = (e: any) =>
  typeof e?.response?.data?.detail === "string"
    ? e.response.data.detail
    : "操作失败，请稍后重试；草稿仍保留";
const historyLabels = {
  active: "有效",
  corrected: "已更正",
  revoked: "已撤销",
  inactive: "已失效",
};

export default function AttendanceCalendar() {
  const { user, systemTimezone } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const root = useRef<HTMLDivElement>(null);
  const { isCompact } = useResponsiveLayout(root);
  const editable = Boolean(user?.permissions.includes("attendance.manage"));
  const allowed =
    editable || Boolean(user?.permissions.includes("personnel.sensitive.view"));
  const initialToday = new Intl.DateTimeFormat("sv-SE", {
    timeZone: systemTimezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  const [month, setMonth] = useState(initialToday.slice(0, 7) + "-01");
  const [ids, setIds] = useState<number[]>(
    params.get("member") ? [Number(params.get("member"))] : [],
  );
  const [people, setPeople] = useState<AttendanceCalendarData["members"]>([]);
  const [departments, setDepartments] = useState<
    Array<{ id: number; name: string; parent_id: number | null }>
  >([]);
  const [department, setDepartment] = useState<number>();
  const [keyword, setKeyword] = useState("");
  const [calendar, setCalendar] = useState<AttendanceCalendarData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [view, setView] = useState<"month" | "list" | null>(null);
  const [expanded, setExpanded] = useState<string[]>([]);
  const [dayDetail, setDayDetail] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [pending, setPending] = useState<{
    payload: ScheduleChange | LeaveChange;
    preview: AttendancePreview;
    kind: "schedule" | "leave";
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(params.get("history") === "1");
  const [history, setHistory] = useState<AttendanceHistoryItem[]>([]);
  const [historyPage, setHistoryPage] = useState(1);
  const [historyMember, setHistoryMember] = useState<number | undefined>(
    params.get("member") ? Number(params.get("member")) : undefined,
  );
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [drag, setDrag] = useState<{
    span: AttendanceSpan;
    mode: "move" | "start" | "end";
    anchor: string;
    leave?: CalendarLeave;
    version: number;
  } | null>(null);
  const [dragMonth, setDragMonth] = useState<string | null>(null);
  const requestSeq = useRef(0),
    peopleSeq = useRef(0),
    historySeq = useRef(0),
    previewSeq = useRef(0);
  const first = dayjs(month).startOf("month").format("YYYY-MM-DD");
  const gridStart = addDays(
    first,
    -((new Date(first + "T00:00:00Z").getUTCDay() + 6) % 7),
  );
  const gridEnd = addDays(gridStart, 41);
  const names = useMemo(
    () =>
      new Map(
        [...people, ...(calendar?.members || [])].map((m) => [m.id, m.name]),
      ),
    [people, calendar],
  );
  const spans = useMemo(
    () => attendanceSpans(calendar?.days || []),
    [calendar],
  );
  const listView = view ? view === "list" : isCompact;
  const writable = editable && Boolean(calendar?.editable);
  const submitting = busy && Boolean(pending);
  const selectedKey = ids.join(",");
  const load = useCallback(async () => {
    if (!allowed) return;
    const seq = ++requestSeq.current;
    setLoading(true);
    setError("");
    try {
      const data = await getAttendanceCalendar(gridStart, gridEnd, ids);
      if (seq === requestSeq.current) setCalendar(data);
    } catch (e) {
      if (seq === requestSeq.current) {
        setError(errorText(e));
        setCalendar(null);
      }
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [gridStart, gridEnd, selectedKey, allowed]);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (!allowed) return;
    const seq = ++peopleSeq.current;
    const timer = setTimeout(() => {
      getAttendanceOptions(keyword, department)
        .then((result) => {
          if (seq === peopleSeq.current) {
            setPeople(result.members);
            setDepartments(result.departments);
          }
        })
        .catch((e) => {
          if (seq === peopleSeq.current) setError(errorText(e));
        });
    }, 250);
    return () => clearTimeout(timer);
  }, [keyword, department, allowed]);
  const reloadHistory = useCallback(async () => {
    if (!historyOpen) return;
    const seq = ++historySeq.current;
    setHistoryLoading(true);
    try {
      const result = await getAttendanceHistory({
        page: historyPage,
        page_size: 50,
        member_id: historyMember,
      });
      if (seq === historySeq.current) {
        setHistory(result.data);
        setHistoryTotal(result.total);
      }
    } catch (e) {
      if (seq === historySeq.current) setError(errorText(e));
    } finally {
      if (seq === historySeq.current) setHistoryLoading(false);
    }
  }, [historyOpen, historyPage, historyMember]);
  useEffect(() => {
    void reloadHistory();
  }, [reloadHistory]);
  const versions = () =>
    Object.fromEntries(
      (calendar?.members || []).map((m) => [m.id, m.attendance_revision]),
    );
  const invalidatePreview = () => {
    ++previewSeq.current;
    setPending(null);
    setBusy(false);
  };
  const openNew = (kind: "schedule" | "leave") => {
    if (!ids.length) return;
    invalidatePreview();
    setEditor({
      kind,
      members: kind === "leave" ? [ids[0]] : ids,
      start: first,
      end: first,
      state: "work",
      action: "create",
      absenceType: "temporary_leave",
      reason: "",
      versions: versions(),
      leaveReason: "",
    });
  };
  const openSpan = (span: AttendanceSpan) => {
    invalidatePreview();
    const leave = calendar?.leaves?.find((l) => l.id === span.leaveId);
    setEditor({
      kind: leave ? "leave" : "schedule",
      members: [span.memberId],
      start: leave?.start_date || span.start,
      end: leave ? leave.end_date || "" : span.end,
      state: span.state === "rest" ? "rest" : "work",
      action: leave ? "correct" : "create",
      absenceType:
        leave?.absence_type === "long_term_leave"
          ? "long_term_leave"
          : "temporary_leave",
      recordId: leave?.id,
      originalStart: span.state === "work" ? span.start : undefined,
      originalEnd: span.state === "work" ? span.end : undefined,
      reason: "",
      versions: versions(),
      leaveReason: "",
    });
  };
  const openHistory = (item: AttendanceHistoryItem) => {
    invalidatePreview();
    setEditor({
      kind: "leave",
      members: [item.member_id],
      start: item.start_date,
      end: item.end_date || "",
      state: "work",
      action: "correct",
      absenceType:
        item.absence_type === "long_term_leave"
          ? "long_term_leave"
          : "temporary_leave",
      recordId: item.id,
      reason: "",
      versions: { [item.member_id]: item.attendance_revision },
      leaveReason: item.reason,
    });
  };
  const updateEditor = (patch: Partial<Editor>) => {
    if (submitting) return;
    invalidatePreview();
    setEditor((e) => (e ? { ...e, ...patch } : e));
  };
  const preview = async () => {
    if (!editor) return;
    const seq = ++previewSeq.current;
    setPending(null);
    setBusy(true);
    setError("");
    const payload: ScheduleChange | LeaveChange =
      editor.kind === "schedule"
        ? {
            request_id: crypto.randomUUID(),
            member_ids: editor.members,
            expected_versions: Object.fromEntries(
              editor.members.map((m) => [m, editor.versions[m]]),
            ),
            start_date: editor.start,
            end_date: editor.end,
            state: editor.state,
            reason: editor.reason,
            original_start: editor.originalStart,
            original_end: editor.originalEnd,
          }
        : {
            request_id: crypto.randomUUID(),
            member_id: editor.members[0],
            expected_version: editor.versions[editor.members[0]],
            record_id: editor.recordId,
            action: editor.action,
            absence_type: editor.absenceType,
            start_date: editor.start,
            end_date:
              editor.action === "create" &&
              editor.absenceType === "long_term_leave"
                ? null
                : editor.absenceType === "long_term_leave"
                  ? editor.end || null
                  : editor.end,
            reason: editor.reason,
            leave_reason: editor.leaveReason,
          };
    try {
      const result = await changeAttendance(editor.kind, payload, true);
      if (seq === previewSeq.current)
        setPending({ kind: editor.kind, payload, preview: result });
    } catch (e) {
      if (seq === previewSeq.current) setError(errorText(e));
    } finally {
      if (seq === previewSeq.current) setBusy(false);
    }
  };
  const commit = async () => {
    if (!pending) return;
    setBusy(true);
    setError("");
    try {
      await changeAttendance(pending.kind, pending.payload);
      setPending(null);
      setEditor(null);
      setNotice(
        "已更新；动态统计按最新安排计算，已保存工作日志及导出文件需显式刷新或重新生成。",
      );
      await load();
      await reloadHistory();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const drop = (target: string) => {
    if (!drag || !writable) return;
    const { span, mode, anchor } = drag;
    const leave = drag.leave;
    const oldStart = leave?.start_date || span.start,
      oldEnd = leave?.end_date || span.end;
    const shift = daysBetween(anchor, target);
    const start =
      mode === "end"
        ? oldStart
        : mode === "start"
          ? target
          : addDays(oldStart, shift);
    const end =
      leave?.absence_type === "long_term_leave"
        ? leave.end_date || ""
        : mode === "start"
          ? oldEnd
          : mode === "end"
            ? target
            : addDays(oldEnd, shift);
    if (end && start > end) {
      setError("结束日期不能早于开始日期");
      setDrag(null);
      return;
    }
    invalidatePreview();
    setEditor({
      kind: leave ? "leave" : "schedule",
      members: [span.memberId],
      start,
      end,
      state: "work",
      action: leave ? "correct" : "create",
      absenceType:
        leave?.absence_type === "long_term_leave"
          ? "long_term_leave"
          : "temporary_leave",
      recordId: leave?.id,
      originalStart: leave ? undefined : span.start,
      originalEnd: leave ? undefined : span.end,
      versions: { [span.memberId]: drag.version },
      reason: "",
      leaveReason: "",
    });
    if (dragMonth) setMonth(dragMonth);
    setDragMonth(null);
    setDrag(null);
  };
  if (!allowed)
    return <Alert type="error" message="需要敏感资料查看或出勤管理权限" />;
  return (
    <div ref={root} className="attendance-page">
      <PageHeader
        title="出勤日历"
        description="工作安排与请假区间；不代表实际打卡到岗。周一至周五默认上班。"
        actions={
          <>
            <Button onClick={() => navigate("/grid-members")}>人员管理</Button>
            <Button onClick={() => navigate("/grid-members/weekend-duty")}>
              双休日备勤
            </Button>
          </>
        }
      />
      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            <Button
              onClick={() => {
                void load();
                void reloadHistory();
              }}
            >
              刷新数据（保留草稿）
            </Button>
          }
        />
      )}
      {notice && (
        <Alert
          type="success"
          message={notice}
          closable
          onClose={() => setNotice("")}
        />
      )}
      {calendar && !calendar.editable && (
        <Alert
          type="warning"
          message="逐日排班尚未迁移，当前只读；请管理员完成出勤迁移后再编辑。"
        />
      )}
      {calendar?.legacy_history_incomplete && (
        <Alert
          type="warning"
          message="所选区间早于完整出勤历史起点；缺少的历史不会补造，统计请结合原始记录核对。"
        />
      )}
      <Panel>
        <div className="attendance-content">
          <ListToolbar
            filters={
              <>
                <Select
                  allowClear
                  placeholder="筛选部门"
                  value={department}
                  style={{ minWidth: 180 }}
                  options={departments.map((d) => ({
                    value: d.id,
                    label: d.name,
                  }))}
                  onChange={setDepartment}
                />
                <Select
                  mode="multiple"
                  showSearch
                  filterOption={false}
                  maxCount={100}
                  maxTagCount="responsive"
                  placeholder="搜索并多选人员（最多 100 人）"
                  value={ids}
                  style={{ minWidth: 260, flex: 1 }}
                  onSearch={setKeyword}
                  options={Array.from(
                    new Map(
                      [...people, ...(calendar?.members || [])].map((p) => [
                        p.id,
                        { value: p.id, label: p.name },
                      ]),
                    ).values(),
                  )}
                  onChange={(next) => {
                    setIds(next);
                    invalidatePreview();
                  }}
                />
              </>
            }
            actions={
              <>
                <Button onClick={() => setHistoryOpen(true)}>
                  请假历史／更正
                </Button>
                <Button
                  disabled={!writable || !ids.length}
                  onClick={() => openNew("schedule")}
                >
                  安排上班／休息
                </Button>
                <Button
                  disabled={!writable || !ids.length}
                  onClick={() => openNew("leave")}
                >
                  登记请假
                </Button>
              </>
            }
          />
          <div className="attendance-month-controls">
            <Button
              onClick={() =>
                setMonth(dayjs(month).subtract(1, "month").format("YYYY-MM-DD"))
              }
            >
              上月
            </Button>
            <DatePicker
              picker="month"
              allowClear={false}
              value={dayjs(month)}
              onChange={(d) =>
                d && setMonth(d.startOf("month").format("YYYY-MM-DD"))
              }
            />
            <Button
              onClick={() =>
                setMonth(dayjs(month).add(1, "month").format("YYYY-MM-DD"))
              }
            >
              下月
            </Button>
            <Button
              onClick={() =>
                setMonth(
                  (calendar?.business_today || initialToday).slice(0, 7) +
                    "-01",
                )
              }
            >
              今天
            </Button>
            <Select
              aria-label="日历视图"
              value={listView ? "list" : "month"}
              onChange={setView}
              options={[
                { value: "month", label: "月历" },
                { value: "list", label: "日期列表" },
              ]}
            />
          </div>
          <div className="attendance-legend">
            {Object.entries(ATTENDANCE_LABELS).map(([key, label]) => (
              <span
                key={key}
                className={`attendance-legend-item attendance-${key}`}
              >
                {label}
              </span>
            ))}
          </div>
          {loading && !drag ? (
            <Spin />
          ) : !ids.length ? (
            <Empty description="请先选择需要查看的人员" />
          ) : listView ? (
            <div className="attendance-date-list">
              {Array.from({ length: dayjs(month).daysInMonth() }, (_, i) =>
                addDays(first, i),
              ).map((day) => (
                <section key={day}>
                  <h3>{day}</h3>
                  {calendar?.days
                    .filter((d) => d.date === day)
                    .map((d) => (
                      <Button
                        key={d.member_id}
                        className={`attendance-bar attendance-${d.state}`}
                        onClick={() => {
                          const span = spans.find(
                            (s) =>
                              s.memberId === d.member_id &&
                              s.start <= day &&
                              s.end >= day,
                          );
                          if (span) openSpan(span);
                        }}
                      >
                        {names.get(d.member_id)} · {ATTENDANCE_LABELS[d.state]}
                        {d.conflict ? " · 排班冲突" : ""}
                        {d.overlapping ? " · 请假重叠" : ""}
                      </Button>
                    ))}
                </section>
              ))}
            </div>
          ) : (
            <div className="attendance-month" aria-label="多人出勤月历">
              <div className="attendance-weekdays">
                {["一", "二", "三", "四", "五", "六", "日"].map((d) => (
                  <span key={d}>周{d}</span>
                ))}
              </div>
              {Array.from({ length: 6 }, (_, w) => {
                const week = addDays(gridStart, w * 7),
                  segments = weekSegments(spans, week);
                const memberOrder = Array.from(
                  new Set(segments.map((s) => s.span.memberId)),
                );
                const visible = expanded.includes(week)
                  ? segments
                  : segments.filter(
                      (s) => memberOrder.indexOf(s.span.memberId) < 8,
                    );
                return (
                  <section className="attendance-week" key={week}>
                    <div className="attendance-week-dates">
                      {Array.from({ length: 7 }, (_, i) => {
                        const day = addDays(week, i);
                        return (
                          <button
                            key={day}
                            className={
                              day.slice(0, 7) === month.slice(0, 7)
                                ? ""
                                : "attendance-outside"
                            }
                            onClick={() => setDayDetail(day)}
                            onDragOver={(e) => e.preventDefault()}
                            onDrop={(e) => {
                              e.preventDefault();
                              drop(day);
                            }}
                          >
                            {day === calendar?.business_today ? "今天 · " : ""}
                            {day.slice(5)}
                            {!expanded.includes(week) &&
                            calendar?.days.filter(
                              (d) =>
                                d.date === day &&
                                memberOrder.indexOf(d.member_id) >= 8,
                            ).length
                              ? ` · 还有 ${calendar.days.filter((d) => d.date === day && memberOrder.indexOf(d.member_id) >= 8).length} 项`
                              : ""}
                          </button>
                        );
                      })}
                    </div>
                    <div className="attendance-week-events">
                      {visible.map((segment, i) => {
                        const { span } = segment,
                          movable =
                            writable &&
                            ["work", "leave", "long_term_leave"].includes(
                              span.state,
                            );
                        const anchor = (event: React.DragEvent) => {
                          const bounds =
                            event.currentTarget.getBoundingClientRect();
                          return addDays(
                            segment.start,
                            Math.min(
                              segment.length - 1,
                              Math.max(
                                0,
                                Math.floor(
                                  ((event.clientX - bounds.left) /
                                    Math.max(1, bounds.width)) *
                                    segment.length,
                                ),
                              ),
                            ),
                          );
                        };
                        return (
                          <div
                            key={`${span.memberId}-${span.start}`}
                            className={`attendance-bar attendance-${span.state}`}
                            style={{
                              gridColumn: `${segment.column} / span ${segment.length}`,
                              gridRow: memberOrder.indexOf(span.memberId) + 1,
                            }}
                            draggable={movable}
                            onDragStart={(e) => {
                              e.dataTransfer.setData(
                                "text/plain",
                                "attendance",
                              );
                              setDrag({
                                span,
                                mode: "move",
                                anchor: anchor(e),
                                leave: calendar?.leaves?.find(
                                  (l) => l.id === span.leaveId,
                                ),
                                version: versions()[span.memberId],
                              });
                            }}
                            onDragEnd={() => {
                              setDrag(null);
                              setDragMonth(null);
                            }}
                          >
                            {movable && !segment.continuesBefore && (
                              <span
                                className="attendance-resize"
                                draggable
                                aria-label="拖动开始日期"
                                onDragStart={(e) => {
                                  e.stopPropagation();
                                  e.dataTransfer.setData(
                                    "text/plain",
                                    "attendance",
                                  );
                                  setDrag({
                                    span,
                                    mode: "start",
                                    anchor: span.start,
                                    leave: calendar?.leaves?.find(
                                      (l) => l.id === span.leaveId,
                                    ),
                                    version: versions()[span.memberId],
                                  });
                                }}
                              >
                                │
                              </span>
                            )}
                            <button
                              onClick={() => openSpan(span)}
                              title={`${names.get(span.memberId)} ${span.start} 至 ${span.end}`}
                            >
                              {segment.continuesBefore ? "← " : ""}
                              {names.get(span.memberId)} ·{" "}
                              {ATTENDANCE_LABELS[span.state]}
                              {span.conflict ? " ⚠ 排班冲突" : ""}
                              {span.overlapping ? " ⚠ 请假重叠" : ""}
                              {segment.continuesAfter ? " →" : ""}
                            </button>
                            {movable &&
                              !segment.continuesAfter &&
                              span.state !== "long_term_leave" && (
                                <span
                                  className="attendance-resize"
                                  draggable
                                  aria-label="拖动结束日期"
                                  onDragStart={(e) => {
                                    e.stopPropagation();
                                    e.dataTransfer.setData(
                                      "text/plain",
                                      "attendance",
                                    );
                                    setDrag({
                                      span,
                                      mode: "end",
                                      anchor: span.end,
                                      leave: calendar?.leaves?.find(
                                        (l) => l.id === span.leaveId,
                                      ),
                                      version: versions()[span.memberId],
                                    });
                                  }}
                                >
                                  │
                                </span>
                              )}
                          </div>
                        );
                      })}
                    </div>
                    {memberOrder.length > 8 && !expanded.includes(week) && (
                      <Button
                        type="link"
                        onClick={() => setExpanded([...expanded, week])}
                      >
                        还有 {memberOrder.length - 8} 人，展开本周
                      </Button>
                    )}
                  </section>
                );
              })}
              {drag && (
                <div className="attendance-drag-navigation">
                  {dragMonth && (
                    <section
                      className="attendance-drag-targets"
                      aria-label="跨月目标日期"
                    >
                      <strong>
                        {dragMonth.slice(0, 7)} · 放到目标日期后预览
                      </strong>
                      {Array.from(
                        { length: dayjs(dragMonth).daysInMonth() },
                        (_, i) => addDays(dragMonth, i),
                      ).map((day) => (
                        <button
                          key={day}
                          onDragOver={(e) => e.preventDefault()}
                          onDrop={(e) => {
                            e.preventDefault();
                            drop(day);
                          }}
                        >
                          {day}
                        </button>
                      ))}
                    </section>
                  )}
                  <Button
                    onDragOver={(e) => e.preventDefault()}
                    onDragEnter={() =>
                      setDragMonth(
                        dayjs(month).subtract(1, "month").format("YYYY-MM-DD"),
                      )
                    }
                  >
                    拖入切换上月
                  </Button>
                  <Button
                    onDragOver={(e) => e.preventDefault()}
                    onDragEnter={() =>
                      setDragMonth(
                        dayjs(month).add(1, "month").format("YYYY-MM-DD"),
                      )
                    }
                  >
                    拖入切换下月
                  </Button>
                </div>
              )}
            </div>
          )}
        </div>
      </Panel>
      <Drawer
        open={Boolean(dayDetail)}
        title={`${dayDetail} · 全部安排`}
        onClose={() => setDayDetail(null)}
      >
        <div className="attendance-content">
          {calendar?.days
            .filter((d) => d.date === dayDetail)
            .map((d) => (
              <Button
                key={d.member_id}
                className={`attendance-bar attendance-${d.state}`}
                onClick={() => {
                  const span = spans.find(
                    (s) =>
                      s.memberId === d.member_id &&
                      s.start <= d.date &&
                      s.end >= d.date,
                  );
                  if (span) openSpan(span);
                }}
              >
                {names.get(d.member_id)} · {ATTENDANCE_LABELS[d.state]}
              </Button>
            ))}
        </div>
      </Drawer>
      <Drawer
        open={historyOpen}
        title="请假历史与更正"
        width="min(96vw,800px)"
        onClose={() => setHistoryOpen(false)}
      >
        <div className="attendance-content">
          <Alert
            type="info"
            message="原记录永久保留；审批来源只更正出勤效力，不改写审批结果。"
          />
          <Select
            allowClear
            showSearch
            placeholder="筛选人员"
            value={historyMember}
            options={people.map((m) => ({ value: m.id, label: m.name }))}
            onChange={(m) => {
              setHistoryMember(m);
              setHistoryPage(1);
            }}
          />
          {historyLoading ? (
            <Spin />
          ) : (
            history.map((item) => (
              <section className="attendance-history-item" key={item.id}>
                <div>
                  <strong>{item.member_name}</strong>{" "}
                  <Tag>{historyLabels[item.state]}</Tag>{" "}
                  {item.absence_type === "long_term_leave"
                    ? "长期离岗"
                    : "请假"}
                </div>
                <div>
                  {item.start_date} 至 {item.end_date || "长期"} · 来源：
                  {item.source}
                  {item.workflow_ticket_id
                    ? ` · 工单 #${item.workflow_ticket_id}`
                    : ""}
                </div>
                {item.reason && <div>原因：{item.reason}</div>}
                {item.correction_reason && (
                  <div>更正原因：{item.correction_reason}</div>
                )}
                {item.corrected_at && (
                  <div>
                    账号 #{item.corrected_by} ·{" "}
                    {formatUTCTime(item.corrected_at, systemTimezone)}
                    {item.superseded_by
                      ? ` · 后续记录 #${item.superseded_by}`
                      : ""}
                  </div>
                )}
                {writable && item.is_active && (
                  <Button onClick={() => openHistory(item)}>
                    更正／撤销
                    {item.absence_type === "long_term_leave"
                      ? "／恢复上班"
                      : ""}
                  </Button>
                )}
              </section>
            ))
          )}
          <Pagination
            current={historyPage}
            total={historyTotal}
            pageSize={50}
            showSizeChanger={false}
            onChange={setHistoryPage}
          />
        </div>
      </Drawer>
      <Modal
        open={Boolean(editor)}
        title={editor?.kind === "leave" ? "请假登记／历史更正" : "调整工作安排"}
        onCancel={() => {
          if (!busy) {
            setEditor(null);
            invalidatePreview();
          }
        }}
        footer={
          <>
            <Button
              aria-label="取消调整"
              disabled={busy}
              onClick={() => {
                setEditor(null);
                invalidatePreview();
              }}
            >
              取消
            </Button>
            <Button disabled={!writable || busy} onClick={() => void preview()}>
              预览影响
            </Button>
            <Button
              aria-label="确认提交"
              type="primary"
              loading={busy && Boolean(pending)}
              disabled={!pending || !writable || busy}
              onClick={() => void commit()}
            >
              确认提交
            </Button>
          </>
        }
      >
        {editor && (
          <div className="attendance-content">
            <div>
              人员：
              {editor.members.map((id) => names.get(id) || `#${id}`).join("、")}
            </div>
            {error && <Alert type="error" message={error} />}
            <Alert
              type="info"
              message="更正会影响相应日期的动态人数和人日统计；已有工作日志需主动刷新。"
            />
            {editor.kind === "leave" ? (
              <Select
                aria-label="请假操作"
                disabled={submitting}
                value={editor.action}
                onChange={(action) => updateEditor({ action })}
                options={
                  editor.recordId
                    ? [
                        { value: "correct", label: "更正日期" },
                        { value: "revoke", label: "撤销整条请假" },
                        ...(editor.absenceType === "long_term_leave"
                          ? [{ value: "resume", label: "恢复上班" }]
                          : []),
                      ]
                    : [{ value: "create", label: "新建请假" }]
                }
              />
            ) : (
              <Select
                aria-label="工作安排"
                disabled={submitting}
                value={editor.state}
                onChange={(state) =>
                  updateEditor({
                    state,
                    ...(state === "rest"
                      ? { originalStart: undefined, originalEnd: undefined }
                      : {}),
                  })
                }
                options={[
                  { value: "work", label: "上班" },
                  { value: "rest", label: "休息" },
                ]}
              />
            )}
            {editor.kind === "leave" && !editor.recordId && (
              <Select
                aria-label="请假类型"
                disabled={submitting}
                value={editor.absenceType}
                onChange={(absenceType) =>
                  updateEditor({
                    absenceType,
                    end:
                      absenceType === "long_term_leave"
                        ? ""
                        : editor.end || editor.start,
                  })
                }
                options={[
                  { value: "temporary_leave", label: "临时请假" },
                  { value: "long_term_leave", label: "长期离岗" },
                ]}
              />
            )}
            {editor.action !== "revoke" && (
              <>
                <label>
                  {editor.action === "resume" ? "恢复上班日期" : "开始日期"}
                  <DatePicker
                    disabled={submitting}
                    allowClear={false}
                    value={editor.start ? dayjs(editor.start) : null}
                    onChange={(d) =>
                      d && updateEditor({ start: d.format("YYYY-MM-DD") })
                    }
                  />
                </label>
                {(editor.kind === "schedule" ||
                  editor.absenceType === "temporary_leave") && (
                  <label>
                    结束日期
                    <DatePicker
                      disabled={submitting}
                      allowClear={false}
                      value={editor.end ? dayjs(editor.end) : null}
                      onChange={(d) =>
                        d && updateEditor({ end: d.format("YYYY-MM-DD") })
                      }
                    />
                  </label>
                )}
              </>
            )}
            {editor.originalStart && (
              <div>
                原上班区间：{editor.originalStart} 至 {editor.originalEnd}
                ；移出部分将安排休息。
              </div>
            )}
            {editor.kind === "leave" && !editor.recordId && (
              <Input.TextArea
                aria-label="请假原因"
                disabled={submitting}
                placeholder="请假原因"
                maxLength={200}
                value={editor.leaveReason}
                onChange={(e) => updateEditor({ leaveReason: e.target.value })}
              />
            )}
            <Input.TextArea
              aria-label="调整原因"
              disabled={submitting}
              placeholder="必填：调整／更正原因"
              maxLength={200}
              value={editor.reason}
              onChange={(e) => updateEditor({ reason: e.target.value })}
            />
            {pending && (
              <div className="attendance-preview">
                <strong>请核对后确认</strong>
                {editor.kind === "leave" && editor.recordId && (
                  <div>
                    原请假记录 #{editor.recordId}；本次
                    {editor.action === "revoke"
                      ? "撤销整条"
                      : editor.action === "resume"
                        ? "恢复上班"
                        : "更正日期"}
                    。
                  </div>
                )}
                {pending.preview.changes?.map((d) => (
                  <div key={`${d.member_id}-${d.date}`}>
                    {names.get(d.member_id) || `#${d.member_id}`} · {d.date}：
                    {ATTENDANCE_LABELS[
                      d.before as keyof typeof ATTENDANCE_LABELS
                    ] || d.before}{" "}
                    →{" "}
                    {
                      ATTENDANCE_LABELS[
                        d.after as keyof typeof ATTENDANCE_LABELS
                      ]
                    }
                  </div>
                ))}
                {pending.preview.before && (
                  <div>
                    原请假：{pending.preview.before.start_date} 至{" "}
                    {pending.preview.before.end_date || "长期"}
                  </div>
                )}
                {pending.preview.after && (
                  <div>
                    调整后请假：{pending.preview.after.start_date} 至{" "}
                    {pending.preview.after.end_date || "长期"}
                  </div>
                )}
                {pending.preview.start_date && (
                  <div>
                    影响区间：{pending.preview.start_date} 至{" "}
                    {pending.preview.end_date || "长期"}
                  </div>
                )}
                <p>{pending.preview.message}</p>
              </div>
            )}
            {editor && (
              <Button
                onClick={async () => {
                  try {
                    await load();
                    const data = await getAttendanceCalendar(
                      gridStart,
                      gridEnd,
                      editor.members,
                    );
                    updateEditor({
                      versions: Object.fromEntries(
                        data.members.map((m) => [m.id, m.attendance_revision]),
                      ),
                    });
                  } catch (e) {
                    setError(errorText(e));
                  }
                }}
              >
                保留草稿并读取最新版本，再预览
              </Button>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
