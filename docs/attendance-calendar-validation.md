# 逐日排班与出勤日历验证记录

验证日期：2026-10-21。开发基线：`11bb2563`。本次仅完成源码与本地验证，未部署或执行真实数据库迁移。

## 已完成

- 逐日上班／休息、双日备勤、工作日调休，统一采用请假优先的出勤计算。
- 请假更正、撤销、长期离岗恢复与审批关联；保留原记录及不可覆盖的变更记录。
- 按人员版本检查并发，按请求编号防重复，批量失败整批回滚。
- 多人月历、日期列表、溢出展开、工作及请假拖动、端点调整、跨月预览确认。
- 人员状态、在线汇总、走访人日、工作日志自动取数使用同一计算服务；已保存快照不自动重写。
- 迁移保留旧表，保存原测量摘要；后续人工变化必须有相应审计，不能仅改来源标签绕过 verify。

## 本地检查

在 `backend/` 执行：

```text
python -m pytest tests/test_attendance_schedule.py tests/test_personnel_attendance.py tests/test_grid_member_leave_history.py tests/test_grid_member_status.py tests/test_report_members.py tests/test_unresolved_metric.py tests/test_work_logs.py tests/test_grid_member_identity_account.py tests/test_workflow_ticket_creation.py tests/test_help_docs.py -q
```

结果：117 项测试及 70 项子断言通过。新事务行为测试使用 SQLite SQL 适配器，不能证明 MySQL 的锁、驱动和 SQL 行为。

在 `frontend/` 执行：

```text
node --experimental-strip-types --test tests/attendanceCalendar.test.ts tests/layoutSpacing.test.ts tests/systemTime.test.ts tests/helpCenter.test.ts tests/dashboardCache.test.ts
npm run test:attendance:browser
npm run build
```

结果：25 项针对性 Node 测试通过；Edge 无头浏览器的六种视口通过；生产构建通过。构建仍有仓库既有的大分块警告。

浏览器使用合成人员、模拟接口，验证了多人选择、预览取消不写入、确认提交、工作横条拖动、请假结束日期拖动、跨月拖动及当天十人完整展开。覆盖 1024×640、1280×720、1280×960、1680×1050 深色、1920×1080、390×844 深色，未出现页面横向溢出或脚本异常。1920 视口采用 DPR 1.25，不能等同于真实 Windows 125% 缩放或安装版客户端验收。

全仓 TypeScript 检查与未修改基线各有 44 条去除行列号后的唯一诊断，本次没有新增诊断；不能将此记录为全仓类型检查通过。变更 Python 编译、帮助文档解析、`git diff --check` 和敏感信息边界检查通过。

## 隔离 MySQL 上线前验收

真实 MySQL 迁移、跨库审批事务、并发锁等待及后端重启验证均未执行。本机没有项目数据库，不使用生产补测。

部署与迁移须另行授权，按 [运维手册](operations.md#逐日出勤迁移) 完成备份、维护窗口与 `measure → migrate --apply → verify`。重点复核：旧记录恰好转换为两条逐日记录、逐日在岗结果一致、重复 apply 不覆盖人工调整、并发更新返回 409、失败无部分提交，以及更正／撤销后真实进程重启不复活。

出勤启用后禁止直接回退到旧单日排班程序；优先向前修复，数据库恢复与程序部署分别授权。
