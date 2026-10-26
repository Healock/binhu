# 实时事件与通知投影域

本 PR 为实时通知和事件流提供可重建的元数据投影，按 run_id 与事件类型维护
事件数、最新 revision 和变化字段数。它不承载 Redis stream 正文、通知内容、
权限受众或业务写入；MySQL outbox、Redis relay 和应用通知仍是权威路径。

Python/Flink 双轨结果可独立对账，失败时保留原通知路径并停止该域派生 sink。
