# 在线来源投影域

本 PR 将在线来源域的可重建元数据投影接入 Kafka/Flink 双轨链路。Flink
只接收任务/来源标识、revision、事件类型和变化字段，不接收姓名、证件号、
手机号、地址正文、备注或照片。`_online_source_projection` 仍是业务权威表，
业务写入、人工地址确认和外部匹配继续由 MySQL/Python 处理。

Flink 输出 `dev_online_source_projection`，Python 对照输出
`dev_online_source_projection_python`。两者按 `(run_id, task_id, source_id)`
幂等写入，并为地址匹配、社区、分配、任务图和关注快照提供最近 revision
提示。查询入口已经接入受控读适配器：只有 Development/Staging 同时配置
`ARCHITECTURE_UPGRADE_DOMAIN=online_source`、
`ARCHITECTURE_UPGRADE_READ_SOURCE=flink`、合法运行号和白名单派生库时才读取该表。
派生表缺行或查询失败时，在回退门禁开启时回到 MySQL；Production 和所有写入仍以
MySQL 权威路径为准。

本域的切换前置条件是 Python/Flink 双轨结果逐条一致、Kafka lag 为零、Flink
checkpoint 稳定、派生表可重建，以及保留 Python worker 的回滚路径。

本分支把隔离域代码、公共 Flink sink 链和查询读门禁接通，但不宣称 Staging 或
Production 已完成切换；真实环境仍需双轨对账、回滚和完整验收证据。
