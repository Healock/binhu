# Kafka/Flink/Redis 直接切换合同

## 当前 PR 实施范围

本 PR 为 `task_metadata` 投影域增加真实的 Staging 引擎切换入口。`apply` 先启动
Python worker 和 Flink 双轨运行；受控 `switch` 会验证当前 run 的 Flink JobGraph、
consumer group 和两个 sink，随后停止 `python-metadata-worker`，把该投影域的活动引擎
记录为 Flink。切换证据写入候选的独立 `switch-*` 目录，失败时写入
`switch-failure.json`，并在已停止 worker 的情况下先取消 Flink job 再恢复 Python worker，
避免半切换状态。`rollback` 会取消 Flink、恢复 Python worker，并把 manifest 恢复为
Python 投影引擎。

这次切换只改变投影计算引擎。应用业务读取和业务写入仍分别以 MySQL 和 MySQL/outbox
为权威；`application_read_source=mysql` 是有意保留的边界，不能把这次投影引擎切换
误报为全业务读取切换。bootstrap 的 `available/active` 状态合同继续描述应用侧能力，
地址匹配、日报、研判、归档和外部平台调用不在本 PR 的切换范围内。

连接池问题作为架构切换后的 Staging 复测项保留，不在本 PR 中伪装成已解决。

## 目的

本合同用于已完成 Dev 双轨对账、待完成 Staging 验收的第一个派生域
（任务元数据投影/计数）。当前只有 Dev 的双轨规模与观察门禁证据；Staging
脱敏副本、页面回归、完整 75 人压测、回滚演练和页面验收仍是切换前置条件，
未完成前不得视为晋级通过。它定义切换前的状态和边界；新架构在任务元数据域可用，
但尚未扩展到全部业务域和全量流量。

本合同不覆盖业务写入、地址匹配、人工确认、研判、日报、归档或外部平台调用。
这些路径继续使用本地 MySQL 业务真相和现有 Python 路径，直到各自有独立的对账证据。

## 切换前

执行前必须把以下信息写入本次发布记录：

- Staging 双轨对账证据路径和摘要；
- 唯一 artifact ID、镜像 digest、提交 SHA 和版本；
- 应用与事件管道的 schema/配置摘要；
- 受控备份目录、文件清单和 SHA-256；
- 回滚开关、旧 Python Worker 镜像和回滚负责人。

备份必须通过现有受控备份入口完成，不能手工执行 SQL。备份校验失败时停止切换。

## 一次切换

1. 在维护窗口核对环境身份、artifact、镜像 digest 和数据库 schema。
2. 启用目标派生读取开关，并停止 Python Worker 对同一派生域的新写入。
3. 保留 Kafka/Flink/Redis 的运行编号、checkpoint、topic 和派生库，不删除旧状态。
4. 立即执行短 smoke：保存一条合成任务事件、确认 Flink 派生结果、确认查询返回、确认队列排空。
   合成事件必须带固定的 `cutover_smoke=true` 标记、当前 `run_id` 和
   `source=staging-cutover-smoke`，只允许写入可销毁的 Staging 隔离数据域；
   事件不得进入真实业务统计、日报、告警聚合或生产导出。执行前记录事件键和
   数据域，执行后先保存派生结果、查询结果、队列排空和对账证据，再通过受控
   清理入口删除事件及其派生记录，并复核该 `run_id` 在业务表、统计表、日报
   和派生存储中无残留。清理失败必须停止后续切换并保留证据。
5. 记录切换开始时间、完成时间、开关值、smoke 标记/运行号和清理结果。

切换入口必须是固定的 Staging gateway 命令；默认流程保持双轨，只有显式执行
`switch <STG-run-id>` 才停止 Python worker。没有实际业务读取入口时，不得把本次投影
引擎切换或双轨报告当作全业务读取切换。

## 观察和回滚

切换后只观察固定指标：保存错误率和延迟、派生延迟、Kafka lag、Flink checkpoint、
Redis 错误、派生队列积压和双轨/权威 MySQL 对账结果。

出现关键接口 5xx、派生结果不一致、checkpoint 连续失败、队列持续增长、Redis 错误或
数据库异常时，关闭开关并恢复 Python Worker。回滚必须记录触发原因、开始/完成时间、
旧镜像 digest、数据一致性结果和未覆盖的失败证据；不能删除 Kafka/Flink/Redis 状态或备份。

## 当前实施边界

当前主线已经包含隔离的事件管道和双轨工具，但尚未把该派生域的业务读取入口接入应用。
Dev 双轨验证已通过，Staging 脱敏副本、页面回归、完整 75 人压测、回滚演练和页面验收
仍未完成。因此本合同可以作为切换发布标准，不能作为“Staging 已验收”或“已完成生产
切换”的证明。业务读取入口接入后，必须先在 Staging 完成上述门禁、一次受控切换和
一次回滚，再进行 Production 发布评估。
