# 平台级后台任务与异步执行重构

本文对应架构树 M，记录平台级后台处理的源码盘点和整体替换路线。它与指令核查是并列重构域；不授权在任何已部署环境中停用 Worker。目标是先列全所有相关后台处理，再把同一业务域的旧派生处理作为一个整体重构、统一切换；不把逐个停用 worker 当成最终方案。问题在整体切换后按证据修复。当前源码盘点尚未覆盖所有 API/字段和运行时环境。

## Worker 盘点

| Worker | 队列与触发点 | 写入内容 | 当前读者与替代边界 | 退役门槛 |
| --- | --- | --- | --- | --- |
| `online_projection_jobs` | `_online_projection_jobs`；任务变更从 `routers/query.py`、`services/online_local_writeback.py` 入队 | 重建 `_online_source_projection`、`_online_task_address_matches`；协调 WorkflowData 任务图；更新队列状态 | 任务列表/详情与查询计数、任务图、登记、无法核实、全链条归档、看护/QMF/居住证匹配、辖区档案和分配责任仍有 MySQL 投影读者。Task Kafka/Flink/Redis 只覆盖其中一部分。 | 每个读者要么迁至语义等价的 Redis 合同，要么明确保留为权威 MySQL 状态；所有需要的字段和状态都能重放；任务/分组水位证明 Redis 已追平。 |
| `online_summary_updates` | `_online_summary_updates`；从 `routers/query.py`、`routers/mobile_tasks.py`、`services/online_local_writeback.py`、`services/fullchain_archive_jobs.py` 入队 | 写 `_daily_task_ledger`、`_daily_task_ledger_runs`、`_daily_report_meta` 和按日期/类型生成的统计表；现在也产生增量 `_online_summary_fact_outbox` 事实或 `rebuild_required` 标记 | `/stats/report`、`/stats/report_range`、`/stats/overview`、概览明细、工作量/人员汇总、报表重建和分配责任计算仍依赖 ledger 或生成表。Kafka 尚无 Flink 汇总消费者。 | 汇总事实必须覆盖修正、删除、carryover、每日基线、考勤分母、历史重建和现有全部输出指标；所有接口/报表消费者迁移并有明确水位后，才能停止旧队列消费。 |
| `local_report_scheduler` | `main.py` 启动；`LOCAL_REPORT_SCHEDULER_ENABLED=true` 时启动刷新并每 600 秒运行；`online_summary_updates` 也调用它生成当日基线 | 将当前业务表复制为每日快照，再生成分汇总、总汇总并写 `_daily_report_meta` | 旧报表 Builder、`/stats/report` 和 `/stats/reports` 仍读快照及生成表。增量事实 outbox 没有替代它。 | 新汇总链路必须能重建快照/carryover 基线和所有 Builder 输出；水位覆盖当前业务日及所有业务类型。 |

## 平台后台处理总清单（源码入口盘点）

静态扫描 `backend/main.py`、`backend/services/` 和隔离环境 lifespan 后，当前可确认的常驻入口、按请求启动的作业以及事件管道消费者如下。这个清单先回答“后台都有什么”；后续还要查运行环境、开关、频率、表读写和业务所有者，不能把静态入口数量误当成服务器实际进程数。

| 类别 | 后台处理 | 启动方式 | 架构处置 |
| --- | --- | --- | --- |
| 旧业务派生 worker | `online_projection_jobs`、`online_summary_updates`、`local_report_scheduler` | Production 启动前两者及 scheduler；Development/Staging 启动前两者，scheduler 受开关控制 | 本次 F 的主要整体替换范围：任务派生状态、任务图、在线数据汇总的生产和读取统一迁入 Kafka/Flink/Redis 后，作为一个整体切换；旧表保留为回滚材料。 |
| 其他常驻运维与维护 scheduler（10 项） | `backup_scheduler`、`workflow_scheduler`、`registry_certificate_scheduler`、`qmf_status_scan_scheduler`、`presence_cleanup_scheduler`、`residence_lookup_scheduler`、`unverifiable_review_scheduler`、`venue_cleanup_scheduler`、`venue_cloud_scheduler`、`performance_sampler` | FastAPI lifespan 的 `asyncio.create_task` | 备份、性能采样、清理、外部只读扫描等不是“旧任务投影 worker”，不应为追求架构统一而盲目搬入 Redis；确认属于业务派生数据的部分才纳入整体替换范围。 |
| 额外 Production 常驻循环 | `txdocs_statistics_monitor` | FastAPI lifespan 启动；受独立配置/数据库运行门禁约束 | 腾讯业务数据源已下线；该项仅作为明示的独立只读统计例外核对，确认实际是否启用及保留价值，不纳入业务新架构读写链。 |
| Development/Staging 常驻循环 | `online_projection_jobs`、`online_summary_updates`、`performance_sampler` | `environment_runtime.isolated_lifespan` 启动 | 前两项属于整体替换对象；性能采样保留为运维观测职责。 |
| 按请求启动的持久化/短时作业 | 备份执行、全链条归档导出、外部只读获取、QMF 状态扫描、登记证书来源扫描、来源写回处理、警务下发发布、辖区标注导出、旧同步任务及 QMF 注册工作 | Router/service 在提交任务记录后 `asyncio.create_task` 执行 | 逐项核实是否有可恢复任务记录、幂等键、租约、重试和重启恢复。它们是业务命令/长任务执行器，不等同于投影 worker；需要统一可靠运行时，但不应把业务动作错误改成 Redis 派生读模型。 |
| 未见主 lifespan 接线的循环入口 | `online_writeback_scheduler`、`photo_sheet_scheduler`、`sync_scheduler`、`diagnostic_worker` 等源码入口 | 当前搜索未发现 Production/隔离 lifespan 直接启动 | 标记为待确认：可能是未使用、由其他入口启动或历史遗留，须查调用点与部署实际后再决定保留/删除。 |
| 新事件管道消费者 | Backend outbox relay、Kafka delivery relay、Python metadata worker、Flink task/summary jobs、Redis relay/bridge | 独立 Compose 服务或受控 pipeline runtime | 属于目标架构组成；要核实清单、消费者组、offset/checkpoint、水位和运行身份，不与 FastAPI 内进程 worker 混数。 |

注意：Production `main.py` 当前有 14 个 `create_task` 常驻入口：3 个业务派生 worker（其中包括 `local_report_scheduler`）、10 个其他运维/维护 scheduler，以及 1 个独立只读统计监控器。静态入口不代表它们全都持续访问 MySQL，也不代表都应由 Kafka/Flink/Redis 替代。F 的重点是消除旧业务派生链及其对旧投影/汇总写入的依赖，同时把有恢复要求的长作业移交给可靠的任务执行合同。

Production 的 `main.py` 和隔离 Development/Staging lifespan 都会无条件启动 `online_projection_jobs` 与 `online_summary_updates`。scheduler 有启停配置，另两个 Worker 目前没有各自的启停开关。单纯增加开关并不能证明可以安全停用，不能先加开关再把“可以关”当成退役完成。

## 整体重构与切换顺序

1. 完成全量清单：每个常驻循环、队列消费者、API 启动作业和事件管道消费者，记录启动点、读写表、触发条件、重试/租约、重启行为、MySQL 连接使用和业务所有者。
2. 划定统一迁移边界：所有任务派生读模型与在线汇总属于一批整体替换；备份、性能采样、资源清理、外部只读扫描等独立运维职责单独保留或另行治理，不因“统一”而硬塞进 Kafka/Flink/Redis。
3. 在旧链路仍正常工作期间，一次性补齐新链路缺失的任务状态、业务汇总、基线/历史、删除/修正、重放、长作业恢复和 API 合同。明确哪些 MySQL 表继续是权威状态，哪些旧表只是派生缓存。
4. 新旧输出按同一权威输入和固定水位进行影子对账；两个实现不得同时写同一派生表。把权限、筛选、排序、统计、导出、归档和长作业语义纳入同一批对账范围。
5. 在 Development 完成整批替代后，再在 Staging 对整批替代做一次受控切换：同一维护窗口切换全套读写路由和消费者，不采用“每个 worker 停一轮、观察一轮”的渐进退役方式。切换前记录回滚材料及明确的健康判据。
6. 切换后发现问题就按证据修复；若新链路故障，则依据事先记录的水位恢复整批旧读写路径。旧表、队列及重建代码在稳定期内保留，不立即物理删除。
7. 整批切换稳定后，统一移除旧 worker 启动、旧派生写入和无用队列调用点；物理删表另行审核。

## 现有恢复能力审计

以下是源码能力，不代表已在 Dev/Staging 或生产数据库验证。当前代码没有一条通用命令可以把三个 Worker 的全部派生状态恢复到指定权威水位。

| 数据面 | 已有源码能力 | 不能据此宣称的能力 | 缺口/证据要求 |
| --- | --- | --- | --- |
| `_online_source_projection` | `services/online_source.py::rebuild_projection` 可从未归档 `_online_source_rows`、有效本地变更和地址/登记关联重算指定 parser 的投影；省略 `row_keys` 时是全 parser 重算，并可协调任务图。 | 不能从此函数恢复独立业务状态表、任务操作历史、已归档来源的完整当前态，或证明 Redis 与 MySQL 投影一致。地址人工确认、人工未匹配和复核状态会从 `_online_task_address_matches` 读取并保留；这说明它不是仅由来源行决定的纯函数。 | 没有固定的 rebuild CLI、run manifest、输入水位/hash、输出计数/摘要或 verify 命令合同。须逐项区分可由来源重算的字段与权威状态/人工状态，并核验任务图的完整范围。 |
| `_online_task_address_matches` | 全量投影重建时会按匹配规则重建建议，并保留符合条件的人工确认、人工未匹配和复核状态；反馈记忆参与建议生成。 | 不能只靠原始来源快照恢复人工确认记录、确认人/时间及冲突处理证据；清空后重建会丢掉这些权威人工状态。 | 将人工确认、人工无匹配、反馈记忆列入权威备份与恢复合同；验证重建不覆盖确认、不丢失人工审计。 |
| `_daily_task_ledger` 与日报分汇总 | `services/report_rebuild.py::preview_effective_workload_rebuild` 可列出现存 snapshot 元数据覆盖的回算目标；`rebuild_effective_workload_history` 按现有快照逐日调用 Builder，以 `reset_ledger=True` 重建 ledger 和汇总表。Ledger 构造会读取当日及前一张快照、OnlineData 当前来源/归档状态、责任归属和已有历史工作量。 | 不能补造缺失日期/类型的快照，也不能从当前业务表还原历史某日快照；不能仅凭 ledger 行数、工作量合计证明与某个 Kafka/Redis 水位等价。跨库当前状态变化还可能影响历史回算。 | 现有 preview 不校验连续日期/业务类型完整性、快照内容 hash 或来源水位；执行按日期/类型推进，不是单一原子批次。需要固定输入快照、责任归属版本、日期覆盖、水位和输出摘要，并增加逐范围 verify。 |
| 每日快照、分汇总与总汇总 | `local_report_scheduler::refresh_local_daily_reports_once` 在共享 consumer lock 下生成当前业务日快照、Builder 分汇总和总汇总；`LOCAL_REPORT_SCHEDULER_ENABLED` 可关闭定时任务。 | 它只刷新当前日，不能恢复历史快照；scheduler 开关只控制刷新任务，不等于 `online_summary_updates` Worker 已退役。单次失败后也没有可据以确认整批快照/分汇总/总汇总原子可见的 generation marker。 | 需定义日期 generation、完整业务类型集合、快照内容摘要、分汇总及总汇总完成标记和失败重跑规则。 |
| `_online_summary_fact_outbox` | 增量更新路径可产生事实或 `rebuild_required` 标记。 | 尚无 Flink reducer/Redis 视图；增量事实自身不能重建 carryover、每日基线和完整历史。 | 先完成基线/修正/删除/回补合同和消费水位，再讨论切换与旧汇总重建。 |

因此，目前可称为“有局部可重复的源码重算路径”，不能称为“具备到权威水位的完整回滚能力”。回滚前至少需要备份并核验人工状态、来源/快照输入边界、责任归属版本、事件 offset/水位、输出摘要和接口读回结果。

## 回滚

回滚是显式、可审计的环境操作，不做自动 fallback：

1. 暂停新消费者，记录 run ID、generation 和最后完整的来源/事件水位。
2. 使用已核验的输入快照/权威状态重建旧投影或汇总，并证明输出达到记录的权威水位。对陈旧表直接重启 Worker 不算回滚；当前已有重建函数尚未提供该水位证明。
3. 只有旧数据通过固定一致性检查后，才把接口切回旧读路径。
4. 从记录的位置恢复旧 Worker 和持久队列；保留 Kafka offset、Redis generation、队列行及诊断证据。
5. 若旧投影无法重建到所需水位，保持新路径并返回明确的不可用结果，不能提供陈旧数据。

## 当前阻塞

- 整个平台 Redis 任务状态和 API 覆盖尚未完成，见 `platform-architecture-tree.md` 的 D 节。
- 在线汇总 Kafka 事实还没有 Flink reducer 或 Redis 视图，事实生产目前只覆盖增量更新。
- `_online_source_projection` 有全量重建函数，但缺少固定重建命令、输入/输出水位和 verify；且独立人工/业务状态不能由来源行推导。
- 日报 ledger 有按现存快照回算的源码工具，但不能重建缺失历史快照，也缺少覆盖和内容摘要门禁。
- 旧 MySQL 汇总表仍是在线接口依赖；尚无切换水位或已验证的旧视图重建记录。
- 当前全量清单仍是静态源码盘点，未通过真实服务器进程/容器清单确认实际运行数量和开关状态。
- 本次没有停用或退役任何 Worker，也没有修改环境。
