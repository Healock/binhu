# Dev Python/Flink 双轨比对

- 状态：第一真实派生域已实现，代码合同和本地测试通过；Dev 实际运行与 7 天门禁待执行
- 派生域：任务元数据投影与计数
- 选择原因：只使用 Kafka v1 的脱敏元数据，能够从事件日志重建；结果可用事件数、变化字段数、最高 revision 和按事件类型计数完整描述，避免把地址正文、人员资料或研判正文带入链路。

## 比对合同

Python worker 和 Flink 作业分别写入独立的输出命名空间。每个结果以
`environment + run_id + task_id + source_id` 定位，并包含最高 `revision`、
`event_count`、`changed_field_count` 以及七类事件计数。事件 ID 去重；同一事件
ID 或同一 revision 出现不同元数据时，记录为不可自动归因的差异并停止签署。
Python worker 使用增量 reducer，不再保存完整事件正文；事件 ID 缓存有界（默认 250,000，覆盖本次 100,000 事件门禁）。Flink
先通过 `dev_unique_events` 对完整事件去重再聚合，避免 Kafka 重投放大事件计数。

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
- 主线 CI：run `34778302779`（提交 `befd1236aeb98882be20bbeda847845da72c319d`）全部通过（2026-09-13）
- 本轮增量 reducer/Flink 去重定向验证：36 passed、22 subtests passed；真实 Flink SQL 运行仍待 Dev 服务器执行
- Dev 候选包生成器：`deploy/environments/event_pipeline/deploy.py`；手动工作流：`.github/workflows/prepare-dev-event-pipeline.yml`。
  候选包已经具备提交、运行编号、镜像摘要、源码清单和 SHA-256 门禁，但当前服务器还没有
  对应的固定 Dev event-pipeline 部署网关，因此尚未上传或启动新的 Python worker。

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

## 2026-09-15：Dev recovery16 实际双轨子验收

recovery15 的 savepoint 恢复失败已完成根因诊断：第一次是恢复路径错误，改正路径后
又确认旧匿名 source operator ID 与新 JobGraph 不兼容。未使用 allowNonRestoredState，
旧 savepoint 与失败日志保留在服务器独立证据目录。recovery16 改为干净 Flink 状态，
使用新的运行编号、nonce 和事件 revision。

实际 Dev 结果：Flink 和 Python 都产生同一任务的
revision=302、event_count=6、changed_field_count=6、saved_count=6，
created/claimed/assigned/reviewed/archived/deleted_count 均为 0；Kafka 投递台账的
六条事件均为 published。只重启 Python worker 后，持久事件 ledger 仍为六条，结果
没有回退或重复，说明重启水合逻辑已生效。

该结果是一次真实 Dev 双轨一致性和 worker 重启子验收，不是 7 天/10 万事件门禁。当前
unattributed_difference_count=0 仅适用于这次 recovery16 子集；需要以新证据编号连续
运行至少 7 天并达到 100,000 条唯一事件，随后才能评估 Staging 晋级。
本轮服务器证据目录为
`/var/lib/binhu-dev-event-pipeline/dev-flink-transition-20260915-recovery16/`；
checkpoint_recovery_verified 仍为 false，不能以干净启动替代 savepoint 恢复门禁。

## 2026-09-15：常驻比较器接入候选

新增 Dev-only `dual-track-monitor` 服务候选实现。服务只读
`Dev_EventPipeline` 中的 Python/Flink 投影和 Python 事件账本，使用独立
`evidence` 命名卷保存脱敏、不可覆盖的比较报告；发现差异时写入告警并将状态
持久化为 `paused`，容器重启不会自动清除暂停状态。Compose 合同包含开发环境标签、
只读根文件系统、资源上限、pids 限制、日志轮换和 Dev 内网约束。

本地 `deploy/tests` 全套 240 项通过（2 项按平台跳过）。候选尚未部署到服务器，
因此常驻监控和 1,000/10,000/100,000 事件量级仍未验收；部署后必须以新的
`dual-track-YYYYMMDD-<nonce>` 证据编号开始，保留现有 100 条证据和失败目录。
`checkpoint_recovery_verified=false` 继续保持。

## 2026-09-15：双轨计时与自动监控启动

旧的 `dev-20260915-metadata08` 作业已在保留作业计划、取消输出和前后作业列表后停止；
recovery16 的 revision 与 metadata 作业继续运行。双轨计时起点登记为
`dev-20260915-recovery16`，起始证据目录为
`/var/lib/binhu-dev-event-pipeline/evidence/dev-20260915-dualtrack-start/`。
起始时两套投影各有 1 条任务投影，Python 事件账本和 Kafka 投递台账各有 6 条记录。

事件口径固定为：在同一个 `run_id` 内按 `event_id` 去重；相同事件 ID 且 canonical
payload 相同只计一次，相同事件 ID 内容冲突属于失败，不计为新增事件。双轨目标是连续
7 天且达到 100,000 条唯一事件，计时期间不得出现未归因差异。

比较器已增加周期监控入口。每次扫描写入新的脱敏报告，差异包含任务 ID 哈希、revision、
字段和 UTC 检测时间；发现差异时写入不可覆盖的 `alert-*.json` 并将状态标记为
`paused`。计时暂停后，必须完成差异归因和修复，并以新的证据编号重新开始，不能清除
或覆盖失败报告。当前只完成起点登记和监控单元测试，100/1,000/10,000/100,000
事件量级仍按顺序待执行。

本轮先完成 100 条量级：`dev-20260915-dualtrack-monitor` 下 Python/Flink 任务投影各
100 条、Python 事件账本 100 条、Kafka 投递台账 100 条且全部 `published`；两边最大
revision 均为 1099。该报告保存在起始证据目录的 `scale-100-report.json`，通过后才
进入下一量级。当前自动比较器已经随 PR #665 合入并部署，但它需要两边脱敏 JSONL
输出作为输入；现有服务器尚未把实时投影导出接入调度器，因此不能把这次计数报告冒充
为 7 天自动比对已经运行。

监控实现提交 d9e35e53；PR #665 的模板章节已补齐。
