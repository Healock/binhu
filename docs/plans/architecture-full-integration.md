# Kafka/Flink 全场景投影集成

本集成分支把 12 个元数据投影域放入同一条非生产事件链：任务元数据、在线来源、
在线汇总、实时事件、日报台账、警务派发、登记/家庭、QMF 采集登记、走访考勤、
流程工单、照片事件和场所/外部统计。事件只包含任务/来源标识、revision、事件类型
和变化字段；照片正文、身份证号、手机号、地址、备注和外部写回内容不进入 Kafka。

`projection_definitions.py` 是投影表结构和 Flink sink 查询的唯一公共定义。Staging
初始化表结构和 Flink JDBC sink 都由它生成，避免投影只被打包却没有部署。每个 sink
与 `dev_revisions`、`dev_task_metadata` 一起加入同一个 `StatementSet`，共享一个 Kafka
source 和当前运行的 consumer group；运行时验收从 `pipeline.sql` 的 `INSERT INTO`
集合读取实际 sink，不再硬编码两个分支。

Python reducer 仅作为非生产双轨对照和回滚材料保留。重复 `event_id` 如果内容不同会
立即失败，避免不同投影悄悄收敛到错误结果。权威业务写入仍由本地 MySQL 事务负责，
Flink 只写可重建的派生表。

本分支完成的是事件链路集成和验收合同修复，不等同于 Production 已切换。应用读取
仍需按每个域完成 revision/freshness 栅栏、权限检查、缺行回退和真实 Staging 验收后，
才能关闭对应的 Python 回退路径。
