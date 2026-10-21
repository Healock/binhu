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

PR 首轮后端 CI 暴露走访汇总测试仍模拟旧人员 SQL，已改为在出勤服务边界提供名册，不再将走访记录误返回给出勤查询。修正后，走访汇总及人员报表的 22 项针对性测试通过；在 `backend/` 执行与 CI 相同的 `python -m unittest discover -s tests`，1,039 项测试全部通过。以上仍不代表真实 MySQL 验证。

在 `frontend/` 执行：

```text
node --experimental-strip-types --test tests/attendanceCalendar.test.ts tests/layoutSpacing.test.ts tests/systemTime.test.ts tests/helpCenter.test.ts tests/dashboardCache.test.ts
npm run test:attendance:browser
npm run build
```

结果：25 项针对性 Node 测试通过；Edge 无头浏览器的六种视口通过；生产构建通过。构建仍有仓库既有的大分块警告。

浏览器使用合成人员、模拟接口，验证了多人选择、预览取消不写入、确认提交、工作横条拖动、请假结束日期拖动、跨月拖动及当天十人完整展开。覆盖 1024×640、1280×720、1280×960、1680×1050 深色、1920×1080、390×844 深色，未出现页面横向溢出或脚本异常。1920 视口采用 DPR 1.25，不能等同于真实 Windows 125% 缩放或安装版客户端验收。

全仓 TypeScript 检查与未修改基线各有 44 条去除行列号后的唯一诊断，本次没有新增诊断；不能将此记录为全仓类型检查通过。变更 Python 编译、帮助文档解析、`git diff --check` 和敏感信息边界检查通过。

## PR #1053 首轮审查修复

针对 `7ce00d27` 的四项审查意见：

- 出勤、岗位配置及名册查询统一限定人员域库名；`PLATFORM_DOMAIN_ACTIVE=false` 使用配置的在线库，开启时使用配置的人员库，调用方为日报连接也不依赖默认库。
- 日历新建长期离岗明确提交 `end_date=null`；切换类型时清空隐藏日期，更正已有长期离岗仍保留原结束日期合同。
- 草稿、打开记录及取消编辑使旧预览失效；晚到响应、错误和结束状态不能恢复旧确认内容。真正提交期间冻结编辑，避免提交内容与表单分离。
- 迁移 `verify` 按每人每日最新变更的状态及版本校验，已有人工修改后恢复迁移初值或较早审计版本均判为不通过。

本轮在 `backend/` 执行：

```text
python -m pytest tests/test_attendance_schedule.py tests/test_report_members.py tests/test_personnel_attendance.py tests/test_personnel_positions.py tests/test_business_time.py tests/test_domain_routing.py tests/test_visit_summary.py -q
```

81 项测试及 2 项子断言通过。新增报表读取用例在空默认库连接上挂载独立 SQLite schema，保留查询库名前缀，分别验证分域开关关闭、开启时休息、请假及正常工作日的名册结果；不把该结果当作 MySQL 驱动、锁或跨库写事务验证。迁移用例增加“人工修改后无审计恢复 legacy 初值”和“恢复较早有效审计版本”两种失败断言。

前端 `npm run test -- --test-name-pattern=attendanceCalendar` 实际执行了完整 Node 套件，443 项通过（该尾部参数未筛选测试，不能将其记为定向运行）。`npm run test:attendance:browser` 六种视口均通过，新增新建长期离岗的预览及提交请求断言，并通过延迟预览响应复现草稿修改竞态，确认旧响应不能启用确认，重新预览和提交使用当前状态及原因。`npm run build` 通过，保留既有分块警告；变更 Python 编译及 `git diff --check` 通过。

本轮仍未执行真实 MySQL、真实进程重启、部署或生产迁移。独立复核与 CI 的结果另行记录，不能由本地通过推断。

## 隔离 MySQL 上线前验收

真实 MySQL 迁移、跨库审批事务、并发锁等待及后端重启验证均未执行。本机没有项目数据库，不使用生产补测。

部署与迁移须另行授权，按 [运维手册](operations.md#逐日出勤迁移) 完成备份、维护窗口与 `measure → migrate --apply → verify`。重点复核：旧记录恰好转换为两条逐日记录、逐日在岗结果一致、重复 apply 不覆盖人工调整、并发更新返回 409、失败无部分提交，以及更正／撤销后真实进程重启不复活。

出勤启用后禁止直接回退到旧单日排班程序；优先向前修复，数据库恢复与程序部署分别授权。
