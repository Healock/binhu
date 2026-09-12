# Dev Kafka、Flink 与 Redis 架构升级

- 当前状态：Dev 元数据事件链路已合入主线，处于开发和验收阶段；未切换 Production，未完成完整业务派生双轨
- 目标：把容易与业务写入抢锁的可重建派生计算逐步移到 Dev 的 Kafka/Flink/Redis 链路，先验证事件合同、版本栅栏、恢复和回放，再决定是否进入 Staging
- 边界：MySQL 继续是业务真相；Dev 只使用虚构或已脱敏数据；不复用 Shadow 数据目录、checkpoint、Redis/Kafka 卷、数据库卷或运行编号

## 当前已具备的基础链路

Dev 已有独立的 `binhu-development-pipeline` 资源定义和事件身份检查：Backend 的受控元数据事件进入 Dev Redis Stream，经业务桥和可靠投递台账写入 Dev Kafka，Schema Registry 校验固定 JSON 合同，Flink 按 `run_id + task_id + source_id` 计算最高 revision，结果写入 `Dev_EventPipeline`，再由 Redis revision fence 提供低延迟读缓存。

这条链路目前只证明元数据传输和 revision 保护。它不包含完整任务正文，不执行生产外部写回，不替代生产 Python 派生 worker，也不能据此宣称日报、地址匹配或研判一致性已经迁移。

## 本轮实施范围

1. **先止血**：研判来源一致性核对和逾期推进改为每次最多处理 100 条，并使用 `FOR UPDATE SKIP LOCKED`。被业务写入占用的记录留给下一轮，避免后台派生检查长时间持有范围锁。批量上限由代码校验，不能用参数恢复无界查询。
2. **固定事件边界**：事件只保留业务类型、稳定任务标识、来源 ID、revision、变化类别和运行编号。姓名、证件号、手机号、完整地址、备注正文、令牌和外部平台正文不得进入 Kafka、Flink 或 Redis 派生库。
3. **完善 Dev 验收**：用合成任务验证重复事件、乱序 revision、桥/relay 重启、Kafka ACK 后台账、Flink checkpoint/savepoint 和 Redis 高水位。失败运行保留独立证据，不覆盖之前的验收材料。
4. **选择第一个派生域**：优先迁移可重建的任务元数据投影或计数，不迁移业务写入、研判决定、地址人工确认、外部平台调用和归档删除。派生结果异常时，查询必须回到权威 MySQL 或明确返回“派生结果暂不可用”。
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
