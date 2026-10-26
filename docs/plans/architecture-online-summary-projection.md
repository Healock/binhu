# 在线汇总投影域

本 PR 将在线摘要刷新活动的可重建计数接入 Kafka/Flink。投影按 UTC 日期和
事件类型统计事件数、任务数和变化字段数，不复制任务正文、人员信息、地址或
汇总业务金额。`_online_summary_updates` 和现有日报表仍由 MySQL/Python 负责，
因此本 PR 不改变业务写入或日报权威路径。

Python 与 Flink 分别写入隔离的 `dev_online_summary_projection_python` 和
`dev_online_summary_projection`，可按 run_id 幂等重建和对账。真实在线汇总读
适配器和 Staging 切换必须在该域单独完成双轨验证后再启用。
