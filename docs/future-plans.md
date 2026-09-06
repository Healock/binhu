# 未来计划

这里记录已经确定方向、但本次还不实施的功能。真正开始开发前，仍要重新核对业务需求和现有代码。

## 事件总线与实时计算基础设施（长期演进，影子阶段）

目标是在保持 MySQL 为唯一业务真相的前提下，逐步建设 Kafka 事件总线、Flink 实时派生、Redis 版本缓存和可观测回放能力。75 人突发复测只作为阶段趋势指标，不作为采用该架构的唯一依据。

当前状态：设计完成，第一阶段实现中。生产业务仍使用 MySQL + Python 派生 worker；Kafka、Flink、Redis 先在隔离影子环境验证。

阶段顺序：

1. 盘点并接入全部业务 Outbox（`_domain_event_outbox`、`photo_sheet_outbox`、`_venue_cloud_outbox` 及后续确认的业务 Outbox）；`_online_projection_jobs` 保持派生队列身份。领域 Outbox 已完成严格元数据转换器和独立 ledger/relay 组件验证，仍未接入真实业务事务；照片同步与场所云 Outbox 先按各自事件合同登记，禁止伪装成任务领域事件。
2. 建立元数据事件合同：`event_id`、事件类型、`task_id`、`source_id`、revision、operation_id、变更字段摘要和时间；禁止完整任务正文及敏感人员资料进入事件。
3. 统一消费者回读接口为 `/internal/v1/derived-input` 版本化 HTTP JSON；消费者禁止自建 SQL。接口使用独立服务凭据、字段白名单、revision fence 和回读审计。
4. 在独立 Compose 项目验证 Kafka KRaft 三节点、Schema Registry、relay、重试/DLQ、故障恢复和回放。三节点只代表协议与故障行为；事件量超过约 10 万/天时另立容量评估。
5. Flink 与 Python worker 双轨运行，Redis 结果必须带 revision；连续 7 天且累计至少 100,000 条事件、零未归因差异后才允许结束双轨观察。任何差异立即阻断并重新计时。
6. 多级缓存：在 Backend 内增加本地缓存层（如 Caffeine），存放字典数据（任务类型、状态枚举、小区列表、核查人选项），减少 Redis 网络 IO；Redis 保留为分布式缓存层，存放用户会话、任务详情投影、列表缓存。本地缓存采用启动时加载 + 定时刷新策略，更新频率极低的元数据全部命中本地缓存。
7. 多实例 + API 网关：Backend 扩展为多个容器实例，由 API 网关（Kong/APISIX）统一接入。网关负责认证前置、限流熔断、TLS 终止、敏感数据脱敏、负载均衡和灰度发布。Backend 实例无状态化，共享 Redis 会话和缓存。
8. 链路追踪 + 持续剖析：接入 SkyWalking（链路追踪）和 Pyroscope（持续剖析）。链路追踪覆盖从网关到 Backend 到 MySQL/Redis 的完整请求路径，支持按 Trace ID 定位慢请求；持续剖析采集 CPU 和内存火焰图，定位热点函数。采样策略按比例或按错误触发，不在生产环境全量开启。
9. 每个阶段完成后关联同口径 75 人复测，记录接口延迟、锁/死锁、Kafka lag、Flink checkpoint、Redis 命中、队列排空和零串写；失败时分别分析事务、查询、消费、派生与缓存。

当前明确不做：不把 Kafka/Flink/Redis 设为最终数据源；不把多级缓存作为唯一数据来源，MySQL 仍是权威数据源；不在无网关的情况下直接暴露多实例；不在生产环境全量开启链路追踪采样；不在生产启用影子入口；不以三节点配置推导生产容量；不恢复腾讯文档路径；不删除 Python worker 回退路径；不宣称跨 Kafka、Flink、Redis、MySQL 的天然 Exactly-Once。

每阶段必须留下状态、阻塞项、下一步、配置/版本、测试命令、故障演练、差异样本、回滚结果和证据目录。恢复工作时先读取本节，再核对实际代码和服务器状态。

### 执行台账（2026-09-06）

用户已授权创建全新隔离影子项目、现场解析镜像 digest、边测试边修补提交；不得操作正式项目或现有 `binhu-loadtest-lt-*`。镜像代理准备采用现有 `docker.1panel.live` 的显式仓库路径，保留 TLS 校验，不重启 Docker、不更改全局 daemon。入口与凭据只保留在本机运维信息中。服务器证据统一存入新项目的 `artifacts/`。

| 阶段 | 当前可核实状态 | 下一步 / 退出证据 |
| --- | --- | --- |
| 镜像准备 | Kafka、Apicurio、Flink 1.20.1 完整镜像均已通过代理取得，固定 digest；Relay 离线镜像已构建 | 所有基础镜像、应用构建和依赖继续保留锁与哈希 |
| Kafka 三节点 | 三业务主题均为 3 分区/2 副本；单 Leader 停止 30 秒后选举、恢复 ISR、旧消息回读及新消息投递均已通过协议烟测 | 协议通过不等于 Relay 业务闭环；服务认证/ACL 尚未实现 |
| Apicurio | 2.6.5.Final 已运行，`/health/ready` 全部 UP；Draft 7 Schema 已注册、回读一致，兼容变更返回 200，不兼容字段类型变更返回 409；`mem` 仅用于协议实验 | 恢复前导入同一版本 schema；持久化 Registry 仍未完成 |
| Outbox → Kafka | 独立投递状态机、真实隔离 MySQL ledger 与 12 条 Kafka 往返已通过；回滚、重复 ID、租约 fencing、分区键均有证据 | 接入真实业务 Outbox 事务、进程崩溃、重试与 DLQ；旧 Redis relay 保留 |
| 回读接口 | 已有骨架和模拟测试，真实 task_id 映射与版本快照待复审 | 鉴权先于取连接、影子范围、真实字段、同一 revision 输入 |
  | Flink / Redis | Flink Kafka checkpoint 协议烟测、Redis revision fence 和真实恢复验证已通过；业务派生、MySQL/Redis 输出和双轨比对仍未开始 | 先接入真实业务事件，再做地址匹配、人员标签、任务图、日报、条件写入和双轨验证 |
| 双轨 | 尚未开始；不得累计假想事件或观察时长 | 独立输出、连续 7 天且至少 100000 个唯一事件，无差异 |
| 多级缓存 | 未开始 | 本地缓存选型、字典数据清单、刷新策略设计；Redis 缓存分层方案 |
| 多实例 + API 网关 | 未开始 | 网关选型、无状态化改造、灰度发布流程设计 |
| 链路追踪 + 持续剖析 | 未开始 | SkyWalking/Pyroscope 影子部署、采样策略、仪表盘设计 |
| 75 人复测 | 本架构尚未执行 | 集成完成后全新卷、75 人/5 分钟，保存原停止线和排空证据 |

### Outbox 全量接入清单（2026-09-07）

| 来源 | 业务性质 | Kafka 处理边界 | 当前状态 |
| --- | --- | --- | --- |
| `_domain_event_outbox` | 任务领域事件 | 使用 `binhu.task.events.v1` 严格元数据合同，回读任务正文 | 转换器、ledger、真实影子投递和 SIGKILL 窗口已验证；尚未挂入 Backend 事务 |
| `photo_sheet_outbox` | 照片名单外部写回意图 | 单独事件类型/主题，保留 work order 与 action 元数据；不得写入任务正文或照片 | 已盘点，独立元数据合同已实现；真实 relay 接入待实现 |
| `_venue_cloud_outbox` | 场所云外部同步意图 | 单独事件类型/主题，保留 venue ID、配置 revision、action、request ID | 已盘点，事件合同和权限/重试边界待实现 |
| `_online_projection_jobs` | 本地派生队列 | 保持独立队列，不转换为领域事件 | 继续由 Python worker 管理，Flink 接入另立阶段 |

全量接入的完成条件是每个来源均有版本化元数据合同、同事务写入/源记录关联、至少一次 relay、有限重试/DLQ、消费者幂等和回放证据；“有 Kafka 主题”不算完成。

可靠性十项固定为：事务 Outbox 与 ACK、Relay 崩溃恢复、单 broker 故障重试、有界退避/DLQ、重复事件幂等、乱序 revision fence、7 天 retention 删除、停写排空、broker/checkpoint 恢复、脱敏归档回放。当前十项均待真实集群验收，不能用单元测试或 Kafka CLI 替代 Backend/Flink 业务闭环。

本次基础设施运行编号为 `KSHADOW-20260906T084957Z-fcbad2`，项目名为 `binhu-kafka-shadow-20260906`。现场证据包括 `deployment-identity.json`、`kafka-shadow-images.lock.json`、`quorum-after-tmpfs.txt` 和三个主题的 `*-describe.txt`。主题显式配置 `retention.ms=604800000`、`min.insync.replicas=2`；这只证明配置，尚未证明自然 7 天删除。Apache 镜像隐含的两个匿名卷已改为有界 tmpfs，并仅重建本次项目容器；数据卷保持项目作用域。当前网络内使用 PLAINTEXT、无宿主机发布端口，不能声称认证故障项已覆盖。

故障演练证据：`KSHADOW-20260906T084957Z-fcbad2-protocol-smoke-attempt-02.json` 为通过结果；首次预检因把 Docker `EXPOSE` 的空绑定误判为宿主机端口而停止，未停 broker，诊断保留在首轮文件。修复后完整重跑，停止当时 Leader 3、恢复后消费原 3 条及新增 3 条合成消息。MySQL 组件证据：`delivery-store-verification-02.log`，仅测试独立 ledger，不代表已有 Backend 业务 Outbox 已接入。初轮容器创建前因 YAML 内 tmpfs 逗号解析错误退出，修复并重跑通过；未对业务数据库写入。

验收解释：至少一次投递允许“Kafka 已 ACK、Outbox 尚未记账”崩溃窗口的重复消息，消费者必须防止重复副作用；已持久化确认的投递不得重新领取。认证错误导致 DLQ 也不可写时，保留本地持久化失败状态，不伪造 Kafka DLQ 成功。7 天配置核对、缩短保留期机制实验和自然经过 7 天的验证分别记录；不修改服务器时间或用短实验代替连续 7 天双轨。任何差异修复后重启观察窗口。同一项连续三次修补失败按用户要求停止并留存诊断。

## 腾讯表时代数据模型退场（0.29.x）

平台已经以本地 MySQL 业务表作为唯一主数据源，但在线任务接口和部分表结构仍保留腾讯表时期的缓存、物理行定位和多重正文副本。`0.28.8` 只停止本地热路径读取腾讯元数据并优化派生队列，不更换生产主键、不删除历史表。

后续开发固定按以下阶段推进：

1. 建立稳定本地 `task_id` 和轻量任务头表；任务正文只保存在对应业务表。
2. API 从缓存意义的 `source_id` 迁移到 `task_id`，兼容期只维护 ID 映射，不复制任务正文。
3. 停止在业务表、`_online_source_rows` 和 `_local_source_records` 之间保存多份 `values_json`、摘要和 revision。
4. 将 `_online_writeback_audit` 迁移为通用任务变更审计；将 `_online_local_changes`、腾讯缓存状态、单元格元数据和物理行字段转为历史只读资料。
5. 清除正常运行路径对 `spreadsheet_id`、`sheet_id`、`physical_row` 和 `cell_meta_json` 的依赖。
6. 每阶段先测量查询和引用范围，备份受影响数据库，再执行分批迁移、双读核验和最终切换；不采用长期双写。
7. 历史腾讯表只在保留期内用于审计和回滚。删除历史表、OAuth 记录或备份必须另行审批，不得夹带在普通功能或补丁版本中。

开始实现前必须重新统计生产表行数、索引、外键式引用、API 使用点和审计保留要求，并为每个阶段提供 `measure → migrate --apply → verify` 工具及独立回滚说明。

## 外部出勤和请假系统对接

平台已经保存多条请假历史和每周双休日备勤，可以按日期计算在岗人日。后续仍未实施的是：

- 保存外部请假单或工单编号，并用外部编号防止重复导入。
- 区分待审批、已生效、已撤销和已结束。
- 自动同步调休、临时加班和周末换班。
- 给历史记录增加更完整的查询、修改和撤销页面。

接入外部系统前，要先确定人员唯一编号、重复数据、撤销请假、日期重叠和换班规则，不能只按姓名覆盖。


### 2026-09-06 Redis revision cache 进展

Redis 版本缓存合同已实现并通过 27 项单测。高水位指针不设置 TTL，版本快照按 TTL 过期，避免快照过期后旧事件覆盖新版本。真实 Redis 大整数 revision、乱序、重复、同版本冲突和快照重建验证待在影子服务器执行；Flink Kafka checkpoint 协议烟测已在影子集群部署；业务派生、MySQL/Redis 输出和双轨比对仍未开始。


### 2026-09-07 影子 Redis 真实验证

真实隔离 Redis 验证已通过：revision=9223372036854775806 写入成功；旧 revision 返回 stale；重复事件返回 duplicate；同 revision 不同内容返回 conflict；高水位 revision 保留，版本快照 TTL=60 秒。证据位于影子服务器 `artifacts/redis-revision-612f5fa9/`。该结果只证明缓存组件合同，不代表 Flink 业务派生或双轨一致性已完成。


### 2026-09-07 Flink Kafka checkpoint 影子验证

- 在固定 digest 的 Flink 1.20.1 Java17 镜像中，Kafka connector JAR 的 SHA-256 为 `1086f3eee73d727e234860fcd03adafc0d76f2fc70a25d39c36427693fff749d`，离线 Java 编译和 16 项严格元数据合同检查通过。
- JobManager/TaskManager 使用独立影子网络和 checkpoint 命名卷启动。首轮提交暴露 checkpoint 卷属主错误；修正为容器用户 9999 后通过。TaskManager 重启时首轮因 5 秒重试间隔短于注册时间失败；保留诊断并将固定重试间隔改为 30 秒，从 checkpoint 22 重新提交。
- 恢复验收通过：作业 `b80fc8cd2d5fb4f59c14f62455487315` 重启前已完成 4 个 checkpoint，TaskManager 重启后恢复计数状态并完成第 5 个 checkpoint；恢复后 revision 3/5/6 分别输出 `STALE`/`DUPLICATE`/`APPLIED`，最高 revision 从 5 到 6。证据位于影子服务器 `artifacts/flink-recovery-02-after.json`、`flink-post-recovery-output.log`。
- 当前结论只覆盖 KafkaSource、元数据解析、checkpoint 和 revision 状态恢复；它不是地址匹配、人员标签、任务图、日报、Redis/MySQL 投影或 Python/Flink 双轨验收。


### 2026-09-07 Outbox 接入准备

新增 `backend/services/kafka_outbox_bridge.py`，将现有 `_domain_event_outbox` 行转换为严格 Kafka v1 元数据；缺少本地 `task_id`、`source_id`、`operation_id`、未登记事件类型或敏感字段摘要的行会被拒绝，不会猜测映射。该模块目前是转换和合同测试，尚未接入生产业务事务或影子 Relay，因此不能称为“全部 Outbox 已接入”。照片同步、场所云 Outbox 和 `_online_projection_jobs` 仍按计划分阶段登记。

- 2026-09-07：再次运行真实隔离 Kafka/derived MySQL ledger 往返：12 条虚构、可清理事件事务提交后被 relay 投递，Kafka 消费到 12 条且 key 与 task_id|source_id 一致，ledger 排空。该测试仍是 ledger-to-Kafka 组件证据，不等价于 Backend 真实事务 Outbox、崩溃窗口、DLQ 或双轨业务验收。


### 2026-09-07 Relay 崩溃窗口真实验证

在独立影子 MySQL/Kafka 上完成真实 SIGKILL 验收：一条已确认事件只投递 1 次且不再领取；另一条在 Kafka ACK 后、ledger 提交前被杀死，90 秒租约过期后重投，消费者收到 2 次，最终 ledger `published` 且 `event_attempts=2`。这验证了至少一次语义与 lease fencing；消费者幂等副作用仍需通过真实派生投影表完成，不能把 Kafka broker 的重复消息当作自动幂等。服务器证据为 `artifacts/relay-crash-verification-01.log`。

