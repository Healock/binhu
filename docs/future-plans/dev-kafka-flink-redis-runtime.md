# Dev Kafka、Flink 与 Redis 架构升级

- 当前状态：Dev 元数据事件链路已合入主线；任务元数据投影/计数已完成 1002 与 10000 条零未归因差异验收，100000 条和连续 7 天观察待执行；恢复门禁待完成，未切换 Production
- 目标：把容易与业务写入抢锁的可重建派生计算逐步移到 Dev 的 Kafka/Flink/Redis 链路，先验证事件合同、版本栅栏、恢复和回放，再决定是否进入 Staging
- 边界：MySQL 继续是业务真相；Dev 只使用虚构或已脱敏数据；不复用 Shadow 数据目录、checkpoint、Redis/Kafka 卷、数据库卷或运行编号

## 当前已具备的基础链路

Dev 已有独立的 `binhu-development-pipeline` 资源定义和事件身份检查：Backend 的受控元数据事件进入 Dev Redis Stream，经业务桥和可靠投递台账写入 Dev Kafka，Schema Registry 校验固定 JSON 合同，Flink 按 `run_id + task_id + source_id` 计算最高 revision，结果写入 `Dev_EventPipeline`，再由 Redis revision fence 提供低延迟读缓存。

这条链路目前只证明元数据传输和 revision 保护。它不包含完整任务正文，不执行生产外部写回，不替代生产 Python 派生 worker，也不能据此宣称日报、地址匹配或研判一致性已经迁移。

## 本轮实施范围

1. **先止血**：研判来源一致性核对和逾期推进改为每次最多处理 100 条，并使用 `FOR UPDATE SKIP LOCKED`。被业务写入占用的记录留给下一轮，避免后台派生检查长时间持有范围锁。批量上限由代码校验，不能用参数恢复无界查询。
2. **固定事件边界**：事件只保留业务类型、稳定任务标识、来源 ID、revision、变化类别和运行编号。姓名、证件号、手机号、完整地址、备注正文、令牌和外部平台正文不得进入 Kafka、Flink 或 Redis 派生库。
3. **完善 Dev 验收**：用合成任务验证重复事件、乱序 revision、桥/relay 重启、Kafka ACK 后台账、Flink checkpoint/savepoint 和 Redis 高水位。失败运行保留独立证据，不覆盖之前的验收材料。
4. **第一个派生域已确定**：选择可重建的任务元数据投影或计数。Python/Flink 两边输出写入独立命名空间，由 `dual_track.py` 按事件 ID、revision 和计数逐条比较；不迁移业务写入、研判决定、地址人工确认、外部平台调用和归档删除。派生结果异常时，查询必须回到权威 MySQL 或明确返回“派生结果暂不可用”。详见 [Dev Python/Flink 双轨比对](dev-dual-track-comparison.md)。
5. **再进入 Staging**：只有 Dev 事件闭环、恢复和脱敏数据门禁全部通过，才用同一提交和同一制品在 Staging 做回归与 75 人趋势复测。Production 继续使用现有 Python 派生路径，直到双轨对账满足单独的退出门槛。

## 资源和隔离门禁

- Compose 项目、网络、数据库、Kafka topic、Schema Registry journal、Redis 卷和 Flink checkpoint 必须带 `development` 身份，并与 Production、Staging、Shadow 不共享。
- Dev 运行编号每次验收重新生成；topic、consumer group、Redis key 和派生库均包含 Dev 命名空间。
- Dev 容器设定 CPU、内存、日志和派生库上限；启动前检查磁盘、内存、端口和卷空间。资源不足或发现生产网络依赖时立即停止。
- 服务器验证使用受控运维连接；本机只做代码、静态配置和不依赖真实数据库的测试。不能把本机模拟结果写成真实 MySQL、Kafka 或 Flink 已通过。

## 回滚和停止条件

Dev 回滚只停止本次 Dev worker、Flink 作业和桥，保留配置、镜像、卷、checkpoint 和失败证据。不能执行宽泛 Docker 清理，不能删除生产回滚镜像或 Shadow 证据。出现环境身份不一致、卷复用、事件跨环境、敏感值泄露、revision 倒退、checkpoint 无法恢复、资源挤占生产或业务写入受影响时，立即停止并回到上一个可用 Dev 版本。

## 晋级标准

只有以下证据齐全，才允许把第一个派生域晋级 Staging：

- Dev 最小事件从 Backend outbox 到 Kafka、Schema Registry、Flink、Redis/派生库可追溯；
- 重复、乱序、重启、重放和 checkpoint/savepoint 恢复均有独立报告；
- 事件合同和敏感字段扫描通过；
- MySQL 业务写入不依赖 Kafka/Flink/Redis 成功，派生故障可安全重建；
- Dev 资源、网络、数据库和卷隔离通过；
- 同一提交、镜像摘要和配置摘要可在 Staging 重放；
- Staging 脱敏副本、权限、回归和压测门禁另有通过证据。

本计划不授权直接修改 Production、建立真实生产双轨、迁移生产研判任务或退役 Shadow。那些动作必须在对应环境验收和发布门禁完成后单独执行。

## 2026-09-14 更新：Backend outbox relay 已接入 Dev

Dev Backend outbox relay 已在服务器部署并验证一条合成业务事件闭环，证据目录为 `dev-backend-outbox-relay-20260914-ef5ddb80`。该结果证明 Backend outbox 能进入 Dev Redis 和现有 Kafka/Flink 派生链路；不代表完整 Dev 11 项、双轨比对、Staging 晋级或 Production 架构切换已通过。relay 仅使用 development 身份和 Dev 专属数据库、Redis、网络与资源限制，Production、Staging、Shadow 未受影响。

当前仍不能标记架构升级完成：完整第 6–11 项恢复与故障演练、7 天/10 万事件双轨比对、Staging 脱敏副本、75 人趋势复测和 Production 切换门禁尚未完成。后续验收 fixture 必须显式使用 UTF-8，避免中文业务类型在受控 SQL 工具中被错误转码。

## 2026-09-14：relay 后 Dev 只读复核与剩余门禁

本轮使用已登记的 `E:\\bhzh-ssh-mcp` 持久 stdio MCP 完成同一会话的服务器只读核验，并主动关闭会话。Dev Backend outbox relay、business-bridge、Kafka 三 broker、Schema Registry、Flink JobManager/TaskManager、派生 MySQL/Redis 和 Backend 均保持运行；Flink 日志显示 savepoint 恢复后持续完成 checkpoint。未执行服务器写入、数据库命令、容器重启或卷操作，Production、Staging、Shadow 未受影响。

这次复核属于运行状态取证，不会把“容器为 running”当作业务验收；业务闭环仍必须以带验收编号的合成事件报告、恢复演练和后续派生域对账为准。

当前可签署范围仍是 Backend outbox → Dev Redis → bridge → Kafka → Schema Registry/Flink → 派生 MySQL/Redis 的合成元数据事件闭环，以及重复/乱序 revision 保护。完整业务派生域尚未接入，仓库也没有可对齐 Python worker 与 Flink 真实业务输出的比较器，因此“7 天、至少 100,000 事件、零未归因差异”尚未开始计时。选择和实现第一个业务派生域会改变架构设计，需单独评审后再推进；在此之前不得把 Dev 标记为完整架构验收通过，也不得开展 Staging 晋级或 Production 切换。

## 2026-09-15：Python/Flink 恢复修复与干净状态验证

第一真实派生域仍为任务元数据投影/计数。PR #661 已将 Python worker 的持久投影水合、
幂等事件账本和 Flink 稳定 UID 修复合入主线；PR #662/#663 为候选构建失败补充了
安全诊断。Dev recovery15 候选部署后，Python worker 已正常运行并完成 schema repair。

Flink savepoint 诊断发现两层问题：第一次提交使用了错误的 dev-flink-transition
根路径，随后改用实际 dev-savepoints 路径后，旧 JAR 和新 UID JAR 都因旧匿名 source
operator ID 无法映射而失败。没有使用 allowNonRestoredState。因此保留旧 savepoint
和失败证据，按 Dev 处置规则建立 dev-20260915-recovery16，启动两个干净的 Flink 作业。

recovery16 的实际结果：两个 Flink 作业为 RUNNING；用新 nonce 和 revision 300/301/302
投递后，Flink 与 Python 的 revision、event_count、changed_field_count 和七类事件计数
完全一致；随后重启一次 Python worker，持久 ledger 和两边结果仍一致。该证据只证明
当前代码的干净启动、事件闭环和 Python 重启恢复，不代表旧 savepoint 兼容恢复已通过。
7 天/10 万事件门禁从 recovery16 的下一轮新事件开始计时，当前状态仍为“待执行”。
失败与恢复材料保存在服务器
`/var/lib/binhu-dev-event-pipeline/dev-flink-transition-20260915-recovery16/`；
旧 metadata08 作业虽仍运行，但按只读 JobGraph 核对仅消费旧 run_id，未与 recovery16
投影键重叠。长跑前仍需再次核对输出命名空间并建立新的证据目录。

## 2026-09-16：monitor21 规模门禁进展

Dev 专属固定网关现已支持受控的 `accept <run_id> <scale>`，规模只允许 1002、10000
和 100000，不接受任意脚本、SQL、路径或 stdin。`monitor21` 使用主线提交
`6f8acd62383edc1c024ff1da013bbe69e34a09b8` 和候选包摘要
`f0bb9a6258f192d773f89708d09e88b83e5c4b1a88428b11f6c1c3552a7821c9` 完成部署。
1002 条验收中 Kafka 投递、Python/Flink 投影、revision 与唯一事件计数全部为 1002，
两类 mismatch 与未归因差异均为 0。第一次 10000 条 run `35068567460` 在 15 分钟时
只发布 5351 条，Python 为 5351、Flink 为 5346，revision mismatch 为 0；同一 run 的
确定性事件随后全部收敛，幂等复核 run `35089738316` 在 24 秒内确认两端与台账均为
10000，projection/revision mismatch 和未归因差异全部为 0。首次超时归因于串行 relay
吞吐，而不是全量后的数据一致性失败。失败证据继续保留。Production、Staging 和
Shadow 未修改。

为进入 100000 条阶段，下一候选只调整 Dev relay：固定 8 个并发 worker、10 条派生库
连接、共享一个幂等 Kafka producer，保留 `SKIP LOCKED`、租约与完成栅栏；并发不能由
环境变量放大，日志按 worker 每 1000 条输出安全计数。候选变化后必须建立新的运行编号，
依次重跑 1002、10000 和 100000。

当前仅允许继续累计规模验收与连续观察。savepoint operator ID 兼容恢复仍未通过，不能
以干净状态启动或 `allowNonRestoredState` 代替恢复门禁；在 100000 条与连续 7 天、
资源/lag/checkpoint 观察及恢复演练全部完成前，不提交 Staging 晋级或 Production
切换结论。
