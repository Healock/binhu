# 事件保留与清理

事件传输记录用于投递、短期重放和故障恢复，不是业务审计档案。业务表、命令记录、正式审计和历史汇总事实按各自的数据保留要求管理，不随事件传输清理。

| 层 | 保留合同 | 清理范围 |
| --- | --- | --- |
| Redis SSE Stream | 最长 7 天，最多 1,000,000 条 | 超期或超量的通知事件可被裁剪；客户端应以业务 API 获取当前事实 |
| Kafka topic | 7 天 | Staging broker 默认值设为 7 天；Development broker 配置、目标 topic 实际配置及自然过期仍需在目标环境验证 |
| `_domain_event_outbox` | 成功发布后 30 天 | 仅 `status='published'` 且 `published_at` 超期的记录，每 6 小时最多删除 200 条 |
| `_kafka_event_delivery` | 成功发布后 30 天 | 仅 `status='published'` 且 `published_at` 超期的记录，每 6 小时最多删除 200 条 |
| 业务审计、命令记录、历史汇总 | 由所属业务合同单独规定 | 不受本策略影响 |

两张 MySQL 传输表的清理都由 `EVENT_TRANSPORT_CLEANUP_ENABLED` 显式开启，默认关闭。开启前必须确认传输台账不是该环境要求保留的业务审计材料，并确认 `idx_domain_event_retention` 或 `published_retention` 索引已通过迁移和 verify。清理按 UTC 时间计算，使用有限批次，失败只输出固定错误码，不影响正常投递。

`pending`、`publishing`、`retry`、`blocked`、`dead_letter`、`dlq_pending`、`dlq_publishing`、`quarantined` 以及其他非 `published` 状态绝不由此任务自动删除。死信和阻塞记录必须由人工处置或另行制定有审计的归档规则。

本策略不执行任何既有环境的数据删除。配置上线前需分别在 Development、Staging 验证索引、清理批次、状态保护和恢复行为；Production 启用需独立确认审计保留要求。30 天是传输台账的建议窗口，不代表业务审计期限。
