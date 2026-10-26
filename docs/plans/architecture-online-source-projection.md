# 在线来源投影域

本 PR 将在线来源域的可重建元数据投影接入 Kafka/Flink 双轨链路。Flink
只接收任务/来源标识、revision、事件类型和变化字段，不接收姓名、证件号、
手机号、地址正文、备注或照片。`_online_source_projection` 仍是业务权威表，
业务写入、人工地址确认和外部匹配继续由 MySQL/Python 处理。

Flink 输出 `dev_online_source_projection`，Python 对照输出
`dev_online_source_projection_python`。两者按 `(run_id, task_id, source_id)`
幂等写入，并为地址匹配、社区、分配、任务图和关注快照提供最近 revision
提示。应用读适配器只有在 development/staging 且显式设置
`ONLINE_SOURCE_PROJECTION_ENGINE=flink` 时才读取派生表；默认仍使用 MySQL
权威路径。派生表缺行时调用方应回退或报告投影滞后，不得将其当作业务事实。

本域的切换前置条件是 Python/Flink 双轨结果逐条一致、Kafka lag 为零、Flink
checkpoint 稳定、派生表可重建，以及保留 Python worker 的回滚路径。
