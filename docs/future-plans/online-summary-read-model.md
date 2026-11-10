# 在线数据汇总读模型重构合同

## 目标

在线数据汇总是平台级 Redis 读模型的业务接入域。它不再把“日报”作为产品概念；`daily_report` 仅是历史数据库名。重构后的汇总由本地权威任务事务产生事件，经过 Kafka 与 Flink 派生到 Redis，Backend 负责权限裁剪、目录名称解析和接口响应。

本合同先覆盖可重建的聚合报表。依赖完整任务正文、原始快照字段、人员出勤目录或文件导出的接口，必须等价读模型和权限合同具备后再切换。

## 现有指标语义

当前实现中的业务日由系统业务时区计算。每个 `parser_type + row_key` 在某个业务日最多贡献一条流水。当天贡献来自：新任务、任务状态或有效地址/结果变化，以及前一日未完成且仍在线的结转任务。

任务状态固定归一为：

- `unchecked`：没有结果，也没有现住址；
- `checked`：有现住址，或结果属于“无法核实”或该业务类型配置的已核查结果；
- `completed`：有非空最终结果。

同一任务已完成状态下的同类别备注变化不重复计工作量。有效工作量只在 `unchecked -> checked` 或 `checked -> completed` 时增加，并受历史累计上限约束。`unable_to_verify` 与 `reached_bottom` 是独立布尔指标。

任务归属使用首个可靠的社区和首个核查人语义；当前接手人不能改写历史贡献归属。正式社区别名在聚合前归一化。社区人员统计只纳入符合当前岗位和社区部门规则的在岗人员。

概览分类保持现有语义：`carryover`、`new`、`changed`、`pending`、`completed`。区间统计按区间内每个任务的最后有效状态去重；移除但未完成的任务不计入最终结果，已完成后移除仍保留完成贡献。

## 汇总事实事件

事件必须能够在不读取敏感正文的情况下重建聚合贡献。建议事件使用以下稳定字段：

```text
contract: binhu.online-summary-fact.v1
event_id, command_id, operation_id
environment, run_id
task_id, parser_type, row_key_hmac
business_date
task_revision, source_revision
first_community_id, first_inspector_id
community_id, inspector_id
state, included, online_present
unable_to_verify, reached_bottom, effective_workload
activity_kind: carryover | new | changed | state_transition | removed
correction: false | true
deleted: false | true
occurred_at
```

`row_key_hmac`、人员和社区标识只能使用环境隔离的 HMAC 或稳定内部 ID。地址、姓名、身份证号、手机号、研判正文和其他敏感正文不得写入汇总事件、Redis 聚合值、日志或 DLQ。事件可以引用任务事件的 `event_id`，但不能依赖某个不可重放的瞬时快照。

Flink 必须按 `event_id` 去重，并按 `task_id + business_date` 实施 revision fence。事件乱序或发现 revision gap 时应标记对应日期/业务类型为 `rebuild_required`，不能把较新的计数直接当成连续、完整的结果。修正和删除采用“移除旧贡献，再加入新贡献”的幂等操作；删除事件必须清理该任务日贡献，但不能删除历史审计材料。

### 当前源码状态

- Backend 已有事实构造器和严格 validator；`online_summary_updates` 在 `_daily_task_ledger` 更新事务内写 `_online_summary_fact_outbox`。功能默认关闭，启用仅允许 Development/Staging，并要求独立 HMAC key。
- 事实 outbox 的结构只能通过 `python -m migrations.online_summary_fact_outbox measure`、`migrate --apply`、`verify` 显式建立和检查；尚未在真实 MySQL 执行。
- revision gap、缺少首归属及无法区分跨日结转/当日变化时写 `rebuild_required`，不发布推测事实。
- Pipeline 现有 `backend-outbox-relay` 在显式启用时可读取同环境 daily_report outbox，按租约投递到 Backend Redis Stream；`business_bridge` 校验事实后写入独立 summary Kafka topic 与既有 delivery ledger。该路径尚未在真实环境验证，daily_report 数据库和账号权限需由 Dev/Staging 环境提供。
- Schema Registry 合同分为 task 与 summary 两个 subject/topic，避免把汇总事实塞进 task event schema。Flink 尚未消费 summary topic，reducer/sink 与 Redis 汇总视图仍未接通。
- 目前事实只覆盖增量更新切片，不能替代每日快照、carryover 基线、考勤人日和历史重建；旧 scheduler 与 worker 继续运行。Redis publish 与数据库确认之间崩溃会造成重复投递，由 Kafka delivery ledger 的 event_id 幂等吸收。
- `command_id` 是该汇总事实的稳定派生标识，不是原 TaskCommand 的命令 ID；它不得被当作原始业务命令关联。

因此，当前实现是事实生产的源码切片，尚非端到端事件链，也不意味着汇总读 API 已切换。

## Redis 视图

首个版本使用环境和运行编号隔离的命名空间：

```text
binhu:online-summary:v1:{environment}:{run_id}
```

聚合主键为 `business_date + parser_type + community_token + inspector_token`。Redis 值保存计数器和版本元数据，不保存业务正文。至少需要支持按日期、业务类型、社区和人员读取：总数、未核查、已核查、已完成、无法核实、见底、有效工作量、完成率和见底率。

每个视图同时保存：`generation`、`last_event_id`、`last_task_revision`、Kafka partition offsets、Flink checkpoint、`complete_through_business_date`、`computed_at`、`lag_state` 和 `rebuild_required`。视图未 ready、存在 revision 冲突、处于重建或水位不足时，API 返回固定 `503 projection_pending`，不能静默读取旧 MySQL 汇总表。

考勤人日、岗位资格、社区别名和权限范围属于目录/权威数据。它们必须以版本或有效时间参与计算；目录缺失时应返回明确的未就绪状态，不能用缓存名称或当前登录人猜测。

## API 切换边界

首个切片目标是让 `/stats/report` 和 `/stats/report_range` 的聚合报表从 Redis 视图读取，并保持现有字段和比例口径，同时增加脱敏的 `source_watermark`。Backend 继续执行用户权限、社区范围、人员范围和字段裁剪。

以下接口暂不切换，直到等价的读模型合同具备：

- `/stats/overview` 与 `/stats/overview/details`：依赖跨日任务去重、分类明细和任务摘要；
- `/stats/today`、`/stats/reports` 和在线汇总导出：依赖文件/快照元数据或完整结果；
- 工作日志和每日明细：还依赖人员出勤、走访及其他业务域；
- 任何需要原始任务正文、地址或敏感字段的详情接口。

旧接口在读模型开关开启且目标视图不可用时必须 fail-closed。只有明确的、审计记录的回滚开关可以恢复旧路径；不能按异常自动回退。

## 重建与退役

重建必须从权威任务事件/受控 backfill 重新生成新的 `run_id` 和 generation，并比较计数、水位和 revision。重建完成前旧 scheduler 与 `online_summary_updates` worker 继续运行；新链路完成等价读回、故障 503、断点恢复和对账后，才可以按 worker→表→API→替代物→开关→回滚点清单停止旧业务写入。

本合同不授权 Production 变更，也不替代 Dev/Staging 的真实链路验收。源码实现、构建制品和环境证据必须分别记录。
