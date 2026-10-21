import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  DatePicker,
  Empty,
  Input,
  Spin,
  Tag,
  Modal,
} from "antd";
import dayjs from "dayjs";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  getWeekendDuty,
  saveWeekendDuty,
  type WeekendDutyBoard,
  type WeekendDutyDay,
} from "../api/client";
import { PageHeader, Panel, ListToolbar } from "../components/ui";
import { useAuth } from "../context/AuthContext";
import "./AttendanceCalendar.css";

export default function WeekendDuty() {
  const { user } = useAuth(),
    navigate = useNavigate(),
    [params] = useSearchParams();
  const editable = Boolean(user?.permissions.includes("attendance.manage"));
  const [week, setWeek] = useState(
    params.get("week") || dayjs().format("YYYY-MM-DD"),
  );
  const [board, setBoard] = useState<WeekendDutyBoard | null>(null);
  const [changes, setChanges] = useState<Record<number, WeekendDutyDay[]>>({});
  const [keyword, setKeyword] = useState("");
  const [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false);
  const seq = useRef(0),
    requestId = useRef(crypto.randomUUID());
  const load = useCallback(async () => {
    const n = ++seq.current;
    setLoading(true);
    try {
      const result = await getWeekendDuty(week);
      if (n === seq.current) {
        setBoard(result);
        setChanges({});
        setError("");
      }
    } catch (e: any) {
      if (n === seq.current)
        setError(e?.response?.data?.detail || "加载失败，请重试");
    } finally {
      if (n === seq.current) setLoading(false);
    }
  }, [week]);
  useEffect(() => {
    void load();
  }, [load]);
  const assign = (id: number, days: WeekendDutyDay[]) => {
    requestId.current = crypto.randomUUID();
    setChanges((c) => ({ ...c, [id]: days }));
    setNotice("");
  };
  const save = async () => {
    if (!board) return;
    setBusy(true);
    setError("");
    try {
      const result = await saveWeekendDuty(
        board.week_start,
        Object.entries(changes).map(([id, days]) => ({
          member_id: Number(id),
          duty_days: days,
          expected_version: board.members.find((m) => m.id === Number(id))!
            .attendance_revision,
        })),
        requestId.current,
      );
      setBoard(result);
      setChanges({});
      requestId.current = crypto.randomUUID();
      setNotice("已保存；双日上班计两个人日，请假优先于工作安排。");
    } catch (e: any) {
      setError(e?.response?.data?.detail || "保存失败，安排草稿仍保留");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="attendance-page">
      <PageHeader
        title="双休日备勤"
        description="周六、周日分别安排，可双日上班或双休；未安排与明确休息不同。"
        actions={
          <>
            <Button onClick={() => navigate("/grid-members")}>人员管理</Button>
            <Button
              onClick={() => navigate("/grid-members/attendance-calendar")}
            >
              出勤日历
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
              onClick={async () => {
                try {
                  const latest = await getWeekendDuty(week);
                  setBoard(latest);
                  setNotice("已读取最新版本，草稿保留；请核对后保存。");
                } catch {
                  setError("刷新失败，请稍后重试");
                }
              }}
            >
              刷新版本，保留草稿
            </Button>
          }
        />
      )}
      {notice && <Alert type="success" message={notice} />}
      <Panel>
        <div className="attendance-content">
          <ListToolbar
            filters={
              <>
                <DatePicker
                  picker="week"
                  allowClear={false}
                  value={dayjs(week)}
                  onChange={(d) => {
                    if (
                      d &&
                      (!Object.keys(changes).length ||
                        window.confirm("切换周次将放弃未保存草稿，是否继续？"))
                    )
                      setWeek(d.format("YYYY-MM-DD"));
                  }}
                />
                <Input
                  placeholder="搜索姓名或社区"
                  value={keyword}
                  onChange={(e) => setKeyword(e.target.value)}
                />
              </>
            }
            actions={
              <>
                {editable && (
                  <>
                    <Button
                      onClick={() => {
                        const next: Record<number, WeekendDutyDay[]> = {};
                        board?.members.forEach((m) => {
                          if (m.previous_recorded)
                            next[m.id] = m.previous_duty_days.filter(
                              (d) => !m.unavailable_days.includes(d),
                            );
                        });
                        requestId.current = crypto.randomUUID();
                        setChanges(next);
                      }}
                    >
                      沿用上周
                    </Button>
                    <Button
                      type="primary"
                      loading={busy}
                      disabled={!Object.keys(changes).length}
                      onClick={() =>
                        Modal.confirm({
                          title: "确认本周排班调整",
                          content: (
                            <div className="attendance-content">
                              {Object.entries(changes).map(([id, days]) => (
                                <div key={id}>
                                  {
                                    board?.members.find(
                                      (m) => m.id === Number(id),
                                    )?.name
                                  }
                                  ：
                                  {days.length === 2
                                    ? "周六及周日上班"
                                    : days.length === 1
                                      ? days[0] === "saturday"
                                        ? "周六上班、周日休息"
                                        : "周六休息、周日上班"
                                      : "周六及周日休息"}
                                </div>
                              ))}
                              <p>将影响对应日期的出勤统计。</p>
                            </div>
                          ),
                          okText: "确认保存",
                          cancelText: "返回修改",
                          onOk: () => save(),
                        })
                      }
                    >
                      保存安排
                    </Button>
                  </>
                )}
              </>
            }
          />
          {loading ? (
            <Spin />
          ) : !board?.members.length ? (
            <Empty description="暂无参与备勤的人员" />
          ) : (
            <>
              <div>
                {board.saturday}（周六）至 {board.sunday}（周日） ·{" "}
                {board.complete
                  ? "排班完整"
                  : `${board.unassigned_count} 人尚未安排`}
              </div>
              <div className="attendance-date-list">
                {board.members
                  .filter(
                    (m) =>
                      !keyword ||
                      m.name.includes(keyword) ||
                      m.community.includes(keyword),
                  )
                  .map((m) => {
                    const days = changes[m.id] ?? m.duty_days,
                      recorded = m.id in changes || m.recorded;
                    return (
                      <section key={m.id}>
                        <div>
                          <strong>{m.name}</strong> · {m.community} ·{" "}
                          {m.position}{" "}
                          <Tag>
                            {m.exempt
                              ? "周末请假"
                              : recorded
                                ? days.length
                                  ? `${days.length} 天上班`
                                  : "双休"
                                : "未排班"}
                          </Tag>
                        </div>
                        <Checkbox.Group
                          value={days}
                          disabled={!editable || busy}
                          onChange={(values) =>
                            assign(m.id, values as WeekendDutyDay[])
                          }
                          options={(["saturday", "sunday"] as const).map(
                            (d) => ({
                              value: d,
                              label: d === "saturday" ? "周六上班" : "周日上班",
                              disabled:
                                m.unavailable_days.includes(d) &&
                                !days.includes(d),
                            }),
                          )}
                        />
                        {editable && (
                          <div>
                            <Button
                              size="small"
                              onClick={() => assign(m.id, [])}
                            >
                              明确双休
                            </Button>
                            {!m.unavailable_days.length && (
                              <Button
                                size="small"
                                onClick={() =>
                                  assign(m.id, ["saturday", "sunday"])
                                }
                              >
                                两天上班
                              </Button>
                            )}
                          </div>
                        )}
                        {!!m.unavailable_days.length && (
                          <span>
                            请假：
                            {m.unavailable_days
                              .map((d) => (d === "saturday" ? "周六" : "周日"))
                              .join("、")}
                          </span>
                        )}
                        {m.conflict && (
                          <Alert
                            type="warning"
                            message="原排班与请假重叠，原安排保留，统计按请假处理；可到出勤日历更正。"
                          />
                        )}
                      </section>
                    );
                  })}
              </div>
            </>
          )}
        </div>
      </Panel>
    </div>
  );
}
