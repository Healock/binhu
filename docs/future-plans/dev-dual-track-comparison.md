# Dev Python/Flink 双轨比对

- 状态：第一真实派生域已实现，代码合同和本地测试通过；Dev 实际运行与 7 天门禁待执行
- 派生域：任务元数据投影与计数
- 选择原因：只使用 Kafka v1 的脱敏元数据，能够从事件日志重建；结果可用事件数、变化字段数、最高 revision 和按事件类型计数完整描述，避免把地址正文、人员资料或研判正文带入链路。

## 比对合同

Python worker 和 Flink 作业分别写入独立的输出命名空间。每个结果以
`environment + run_id + task_id + source_id` 定位，并包含最高 `revision`、
`event_count`、`changed_field_count` 以及七类事件计数。事件 ID 去重；同一事件
ID 或同一 revision 出现不同元数据时，记录为不可自动归因的差异并停止签署。

`deploy/environments/event_pipeline/dual_track.py` 读取两边的脱敏 JSONL 输出，
只把任务 ID 的 SHA-256、revision、字段名和两边的计数写入差异台账。台账不保存
姓名、手机号、证件号、地址、备注、令牌或事件正文。每次运行使用新的
`dual-track-YYYYMMDD-<nonce>` 证据编号，旧目录不可覆盖。

## 当前证据

- 事件合同和投影 reducer：`services/task_metadata_projection.py`
- Flink JDBC 输出：`dev_task_metadata`（由 `flink_sql.py` 创建）
- Dev 派生库初始化：`prepare.py`
- 比对器与脱敏差异台账：`dual_track.py`
- 本地单元测试：`deploy/tests/test_task_metadata_projection.py`
- 当前验证：`deploy/tests` 全套 211 passed、2 skipped、94 subtests passed（2026-09-14）

本次代码验证的是合同、去重、revision 冲突和差异脱敏；本机没有 Docker、
Kafka、Flink 或真实 MySQL，不能把本地测试写成服务器验收。服务器运行时必须
分别启动 Python worker 和 Flink 作业，确认输出命名空间独立，再开始 7 天、至少
100,000 条唯一事件的连续比对。

## 通过门槛

只有连续 7 天、至少 100,000 条唯一事件，且差异台账中
`unattributed_difference_count=0`，同时事件延迟、重复、乱序、worker 重启和
checkpoint/savepoint 恢复均有证据，才能把该派生域标记为“已通过”并提交
Staging 晋级评估。任何真实组件行为偏离预期、数据一致性失败或无法自动归因的
差异都必须保留证据并暂停晋级。

后续迁移顺序暂定为：地址匹配只读派生 → 任务图 → 人员标签 → 日报/汇总。每个
域都要建立独立事件字段、结果表、比对合同和退出门禁，不能把本域的通过结果
直接当作其他域已验收。
