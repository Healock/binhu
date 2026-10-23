import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  DatePicker,
  Empty,
  Input,
  Modal,
  Select,
  Spin,
  Tag,
} from "antd";
import dayjs from "dayjs";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  changeWeeklySchedule,
  getWeekendDuty,
  type WeekendDutyBoard,
  type WeeklyScheduleAssignment,
  type WeeklyScheduleState,
} from "../api/client";
import { PageHeader, Panel, ListToolbar } from "../components/ui";
import { useAuth } from "../context/AuthContext";
import "./AttendanceCalendar.css";

type DraftState = "work" | "rest" | null;
type DayDraft = Record<string, DraftState>;

function mondayOf(value: dayjs.Dayjs) {
  return value.subtract((value.day() + 6) % 7, "day").format("YYYY-MM-DD");
}

const dayNames = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const labels: Record<WeeklyScheduleState, string> = {
  work: "上班",
  rest: "休息",
  unassigned: "未安排",
  leave: "请假",
  long_term_leave: "长期离岗",
};

export default function WeekendDuty() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const editable = Boolean(user?.permissions.includes("attendance.manage"));
  const [week, setWeek] = useState(() =>
    mondayOf(dayjs(params.get("week") || undefined)),
  );
  const [board, setBoard] = useState<WeekendDutyBoard | null>(null);
  const [draft, setDraft] = useState<Record<number, DayDraft>>({});
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [selectedDays, setSelectedDays] = useState<Set<number>>(new Set());
  const [keyword, setKeyword] = useState("");
  const [community, setCommunity] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [patternOpen, setPatternOpen] = useState(false);
  const [pattern, setPattern] = useState<Record<number, "default" | "work" | "rest">>(
    Object.fromEntries(Array.from({ length: 7 }, (_, day) => [day, "default"])),
  );
  const requestSeq = useRef(0);
  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError("");
    try {
      const result = await getWeekendDuty(week);
      if (seq === requestSeq.current) {
        setBoard(result);
        setDraft({});
        setSelected(new Set());
        setSelectedDays(new Set());
      }
    } catch (reason) {
      if (seq === requestSeq.current) {
        setError(
          (reason as { response?: { data?: { detail?: string } } })
            .response?.data?.detail || "加载失败，请重试",
        );
      }
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [week]);
  useEffect(() => {
    void load();
  }, [load]);

  const communities = useMemo(
    () => Array.from(new Set((board?.members || []).map((member) => member.community))).sort(),
    [board],
  );
  const visibleMembers = useMemo(
    () =>
      (board?.members || []).filter(
        (member) =>
          (!keyword || `${member.name}${member.community}`.includes(keyword)) &&
          (!community || member.community.split("、").includes(community)),
      ),
    [board, community, keyword],
  );
  const visibleSelected = visibleMembers.filter((member) => selected.has(member.id)).length;
  const dayDates = useMemo(
    () => Array.from({ length: 7 }, (_, index) => dayjs(week).add(index, "day")),
    [week],
  );

  const stateFor = (memberId: number, dayIndex: number): WeeklyScheduleState => {
    const member = board?.members.find((item) => item.id === memberId);
    if (!member) return "unassigned";
    const key = member.week_days[dayIndex].date;
    const override = draft[memberId] || {};
    if (Object.prototype.hasOwnProperty.call(override, key)) {
      const state = override[key];
      if (state === null) return dayIndex < 5 ? "work" : "unassigned";
      return state;
    }
    return member.week_days[dayIndex].state;
  };

  const setDraftState = (memberId: number, dayIndex: number, state: DraftState) => {
    const date = dayDates[dayIndex].format("YYYY-MM-DD");
    setDraft((current) => ({
      ...current,
      [memberId]: { ...current[memberId], [date]: state },
    }));
    setNotice("");
  };

  const setSelectedDaysState = (state: DraftState) => {
    if (!selected.size || !selectedDays.size) {
      setNotice("先选择人员和日期，再执行批量调整");
      return;
    }
    setDraft((current) => {
      const next = { ...current };
      selected.forEach((memberId) => {
        const member = board?.members.find((item) => item.id === memberId);
        if (!member) return;
        const memberDraft = { ...(next[memberId] || {}) };
        selectedDays.forEach((dayIndex) => {
          const date = dayDates[dayIndex].format("YYYY-MM-DD");
          memberDraft[date] = state;
        });
        next[memberId] = memberDraft;
      });
      return next;
    });
    setNotice("批量调整已加入草稿；请假日期保存时会自动跳过");
  };

  const applyPattern = () => {
    if (!selected.size) {
      setNotice("先选择要套用周模式的人员");
      setPatternOpen(false);
      return;
    }
    setDraft((current) => {
      const next = { ...current };
      selected.forEach((memberId) => {
        const memberDraft = { ...(next[memberId] || {}) };
        dayDates.forEach((day, dayIndex) => {
          const mode = pattern[dayIndex];
          memberDraft[day.format("YYYY-MM-DD")] = mode === "default"
            ? null
            : mode;
        });
        next[memberId] = memberDraft;
      });
      return next;
    });
    setSelected(new Set());
    setNotice("周模式已加入草稿；请假日期保存时会自动跳过");
    setPatternOpen(false);
  };

  const save = async () => {
    if (!board) return;
    const assignments: WeeklyScheduleAssignment[] = Object.entries(draft)
      .map(([id, days]) => ({
        member_id: Number(id),
        expected_version: board.members.find((member) => member.id === Number(id))!
          .attendance_revision,
        days: Object.entries(days).map(([date, state]) => ({ date, state })),
      }))
      .filter((assignment) => assignment.days.length > 0);
    if (!assignments.length) return;
    setBusy(true);
    setError("");
    try {
      const preview = await changeWeeklySchedule(board.week_start, assignments, true);
      const previewRows = preview.changes.slice(0, 12).map((change) => {
        const member = board.members.find((item) => item.id === change.member_id);
        return `${member?.name || "人员"} · ${change.date}：${labels[change.before]} → ${labels[change.after]}`;
      });
      Modal.confirm({
        title: "确认本周排班调整",
        content: (
          <div className="attendance-content">
            <div>将调整 {preview.changes.length} 个个人日期；请假日期跳过 {preview.skipped.length} 项。</div>
            {previewRows.map((row) => <div key={row}>{row}</div>)}
            {preview.changes.length > previewRows.length && (
              <div>以及其他 {preview.changes.length - previewRows.length} 项</div>
            )}
            <div>本次批量变更会在同一事务内保存。</div>
          </div>
        ),
        okText: "确认保存",
        cancelText: "返回修改",
        onOk: async () => {
          setBusy(true);
          setError("");
          try {
            await changeWeeklySchedule(board.week_start, assignments);
            setDraft({});
            setNotice(`排班已保存；请假日期跳过 ${preview.skipped.length} 项`);
            await load();
          } catch (reason) {
            setError(
              (reason as { response?: { data?: { detail?: string } } })
                .response?.data?.detail || "保存失败，草稿仍保留，请刷新数据后重试",
            );
            throw reason;
          } finally {
            setBusy(false);
          }
        },
      });
    } catch (reason) {
      setError(
        (reason as { response?: { data?: { detail?: string } } })
          .response?.data?.detail || "预览失败，草稿仍保留",
      );
    } finally {
      setBusy(false);
    }
  };

  const changedCount = Object.values(draft).reduce(
    (total, days) => total + Object.keys(days).length,
    0,
  );
  const dayTotals = dayDates.map((_, dayIndex) =>
    (board?.members || []).reduce(
      (total, member) => total + Number(stateFor(member.id, dayIndex) === "work"),
      0,
    ),
  );

  return (
    <div className="attendance-page">
      <PageHeader
        title="人员周排班"
        description="工作日默认上班；周末按备勤要求安排。请假记录优先显示，不会被排班覆盖。"
        actions={(
          <>
            <Button onClick={() => navigate("/grid-members")}>人员管理</Button>
            <Button onClick={() => navigate("/grid-members/attendance-calendar")}>出勤日历</Button>
          </>
        )}
      />
      {error && <Alert type="error" showIcon message={error} />}
      {notice && <Alert type="info" showIcon message={notice} />}
      <Panel>
        <div className="attendance-content">
          <ListToolbar
            filters={(
              <>
                <DatePicker
                  picker="week"
                  allowClear={false}
                  value={dayjs(week)}
                  onChange={(value) => {
                    if (value && (!changedCount || window.confirm("切换周次会放弃当前草稿，是否继续？"))) {
                      setWeek(mondayOf(value));
                    }
                  }}
                />
                <Input
                  allowClear
                  placeholder="搜索姓名"
                  value={keyword}
                  onChange={(event) => setKeyword(event.target.value)}
                />
                <Select
                  allowClear
                  placeholder="全部社区"
                  value={community || undefined}
                  onChange={(value) => setCommunity(value || "")}
                  options={communities.map((value) => ({ value, label: value }))}
                />
              </>
            )}
            actions={(
              <>
                {editable && (
                  <>
                    <Button onClick={() => {
                      setPattern(Object.fromEntries(Array.from({ length: 7 }, (_, day) => [day, "default"])));
                      setPatternOpen(true);
                    }}>套用整周模式</Button>
                    <Button
                      type="primary"
                      loading={busy}
                      disabled={!changedCount}
                      onClick={() => void save()}
                    >
                      保存草稿{changedCount ? `（${changedCount}项）` : ""}
                    </Button>
                  </>
                )}
              </>
            )}
          />

          {editable && (
            <div className="attendance-bulk-toolbar">
              <div className="attendance-bulk-selection">
                <Checkbox
                  checked={visibleMembers.length > 0 && visibleSelected === visibleMembers.length}
                  indeterminate={visibleSelected > 0 && visibleSelected < visibleMembers.length}
                  disabled={!visibleMembers.length || busy}
                  onChange={(event) => setSelected((current) => {
                    const next = new Set(current);
                    visibleMembers.forEach((member) => {
                      if (event.target.checked) next.add(member.id);
                      else next.delete(member.id);
                    });
                    return next;
                  })}
                >
                  全选筛选结果（{visibleMembers.length}人）
                </Checkbox>
                <span>已选 {selected.size} 人 · {selectedDays.size} 天</span>
                {selected.size > 0 && (
                  <Button size="small" onClick={() => setSelected(new Set())}>清空人员</Button>
                )}
              </div>
              <div className="attendance-bulk-actions">
                <span>对所选日期</span>
                <Button disabled={!selected.size || !selectedDays.size} onClick={() => setSelectedDaysState("work")}>设为上班</Button>
                <Button disabled={!selected.size || !selectedDays.size} onClick={() => setSelectedDaysState("rest")}>设为休息</Button>
                <Button disabled={!selected.size || !selectedDays.size} onClick={() => setSelectedDaysState(null)}>恢复默认</Button>
              </div>
            </div>
          )}

          {loading ? <Spin /> : !board?.members.length ? (
            <Empty description="暂无参与排班的人员" />
          ) : (
            <>
              <div className="weekly-roster-summary">
                <span>{board.week_start} 至 {dayDates[6].format("YYYY-MM-DD")}</span>
                <span>周末待安排 {board.unassigned_count} 人</span>
                <span>本周变更 {changedCount} 项</span>
              </div>
              <div className="attendance-weekly-table-scroll">
                <table className="attendance-weekly-table">
                  <thead>
                    <tr>
                      <th className="weekly-select-col"><Checkbox
                        checked={visibleMembers.length > 0 && visibleSelected === visibleMembers.length}
                        indeterminate={visibleSelected > 0 && visibleSelected < visibleMembers.length}
                        aria-label="全选筛选结果"
                        onChange={(event) => setSelected((current) => {
                          const next = new Set(current);
                          visibleMembers.forEach((member) => event.target.checked ? next.add(member.id) : next.delete(member.id));
                          return next;
                        })}
                      /></th>
                      <th className="weekly-person-col">人员</th>
                      {dayDates.map((day, index) => (
                        <th key={index}>
                          <span>{dayNames[index]}</span>
                          <small>{day.format("MM/DD")} · {index < 5 ? "默认上班" : "待安排"}</small>
                          <Checkbox
                            checked={selectedDays.has(index)}
                            disabled={busy}
                            aria-label={`选择${dayNames[index]}`}
                            onChange={(event) => setSelectedDays((current) => {
                              const next = new Set(current);
                              if (event.target.checked) next.add(index);
                              else next.delete(index);
                              return next;
                            })}
                          />
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {visibleMembers.map((member) => (
                      <tr key={member.id}>
                        <td className="weekly-select-col"><Checkbox
                          checked={selected.has(member.id)}
                          disabled={busy}
                          aria-label={`选择${member.name}`}
                          onChange={(event) => setSelected((current) => {
                            const next = new Set(current);
                            if (event.target.checked) next.add(member.id);
                            else next.delete(member.id);
                            return next;
                          })}
                        /></td>
                        <td className="weekly-person-col">
                          <strong>{member.name}</strong>
                          <span>{member.community} · {member.position}</span>
                        </td>
                        {dayDates.map((day, index) => {
                          const original = member.week_days[index];
                          const state = stateFor(member.id, index);
                          const isDraft = Object.prototype.hasOwnProperty.call(
                            draft[member.id] || {}, day.format("YYYY-MM-DD"),
                          );
                          const leave = state === "leave" || state === "long_term_leave";
                          return (
                            <td key={day.format("YYYY-MM-DD")}>
                              <button
                                type="button"
                                className={`weekly-state weekly-state--${state} ${isDraft ? "is-draft" : ""}`}
                                disabled={!editable || busy || leave}
                                onClick={() => {
                                  const next = state === "work"
                                    ? "rest"
                                    : state === "rest" ? null : "work";
                                  setDraftState(member.id, index, next);
                                }}
                                title={`${member.name} ${dayNames[index]} ${labels[state]}，点击调整`}
                              >
                                <strong>{labels[state]}</strong>
                                <small>{leave ? "出勤记录" : isDraft ? "待保存" : original.source === "default" ? "默认" : "已安排"}</small>
                              </button>
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="attendance-legend">
                <span className="attendance-legend-item attendance-work">上班</span>
                <span className="attendance-legend-item attendance-rest">休息</span>
                <span className="attendance-legend-item attendance-unassigned">未安排</span>
                <span className="attendance-legend-item attendance-leave">请假 / 长期离岗</span>
                <Tag color="blue">每天 {dayTotals.join(" / ")} 人上班</Tag>
              </div>
            </>
          )}
        </div>
      </Panel>

      <Modal
        title={`套用整周模式 · 已选 ${selected.size} 人`}
        open={patternOpen}
        onCancel={() => setPatternOpen(false)}
        onOk={applyPattern}
        okText="加入草稿"
        cancelText="取消"
        width={720}
      >
        <div className="attendance-content">
          <div className="weekly-pattern-presets">
            <Button onClick={() => setPattern(Object.fromEntries(Array.from({ length: 7 }, (_, day) => [day, day < 5 ? "work" : "default"]))) }>工作日上班</Button>
            <Button onClick={() => setPattern(Object.fromEntries(Array.from({ length: 7 }, (_, day) => [day, day < 6 ? "work" : "default"]))) }>周一至周六</Button>
            <Button onClick={() => setPattern(Object.fromEntries(Array.from({ length: 7 }, (_, day) => [day, "work"]))) }>七天上班</Button>
            <Button onClick={() => setPattern(Object.fromEntries(Array.from({ length: 7 }, (_, day) => [day, day < 5 ? "rest" : "default"]))) }>工作日休息</Button>
          </div>
          <div className="weekly-pattern-grid">
            {dayDates.map((day, index) => (
              <label key={index}>
                <strong>{dayNames[index]}</strong>
                <Select
                  value={pattern[index]}
                  onChange={(value) => setPattern((current) => ({ ...current, [index]: value }))}
                  options={[
                    { value: "default", label: index < 5 ? "默认上班" : "未安排" },
                    { value: "work", label: "上班" },
                    { value: "rest", label: "休息" },
                  ]}
                />
              </label>
            ))}
          </div>
          <div className="weekly-pattern-note">请假日期会被跳过。周末恢复默认后仍是“未安排”，不会自动记为双休。</div>
        </div>
      </Modal>
    </div>
  );
}
