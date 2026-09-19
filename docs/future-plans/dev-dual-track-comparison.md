# Dev Python/Flink 双轨比对

- 状态：第一真实派生域已在 Dev monitor40 完成 1002、10000、100000 三档零未归因差异验收；按项目管理人确认，原连续 7 天观察改为 6 小时强化观察，checkpoint/savepoint 恢复门禁仍未通过
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
分别启动 Python worker 和 Flink 作业，确认输出命名空间独立，再执行至少
100,000 条唯一事件的规模比对和固定 6 小时强化观察。

## 通过门槛

核心双轨规模门禁要求至少 100,000 条唯一事件且差异台账中
`unattributed_difference_count=0`。持续运行门禁按项目管理人 2026-09-20 的明确决定，
从原连续 7 天缩短为 6 小时强化观察；每 30 分钟记录资源、连接、lag、checkpoint、
磁盘、日志轮换和差异，共 13 个采样点。6 小时无异常和增长趋势后可以进入 Staging
晋级评估。checkpoint/savepoint 兼容恢复继续作为独立门禁记录，不得使用
`allowNonRestoredState` 掩盖。任何真实组件行为偏离预期、数据一致性失败或无法自动
归因的差异都必须保留证据并暂停晋级。

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

部署后诊断发现：旧 worker 镜像不包含新 `dual-track-monitor` 命令，容器因此退出；
这属于候选源码与固定镜像入口不同步，不是双轨数据差异。修复方案是在 monitor
容器中只读挂载候选包的 `runtime.py` 与 `dual_track_monitor.py`，其余服务继续使用
原有不可变镜像和数据卷。首次部署失败容器日志已保留，未投递新的量级事件；修复
候选需重新执行 CI、prepare、measure、apply 后才能继续验收。

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

## 2026-09-16：Flink 单 JobGraph 双分支合同

`monitor08` 的事件已经进入 Kafka，但服务器仍运行旧 run_id 的 JobGraph；补齐运行编号
提交门禁后，`monitor10` 又暴露出提交模型问题：同一个 REST 应用先启动
`dev_revisions` 流式 INSERT 后，第二个 `dev_task_metadata` INSERT 无法继续提交。
把两个 INSERT 拆成独立 JobGraph 并复用同一个 consumer group 会由 Kafka 分摊分区，
两边无法同时获得完整事件流，因此不作为修复方案。

经确认，合同调整为一个 RUNNING JobGraph，内部使用同一个 Kafka source 分出
`dev_revisions` 与 `dev_task_metadata` 两个 sink 分支。consumer group 仍严格为
`<run_id>-flink`；Flink REST 验收要求一个作业同时包含两个受控 sink，并继续校验
run_id、development 环境过滤和固定 Dev topic。该选择只用于 Dev 双轨链路，保留现有
checkpoint/savepoint 卷，不使用 `allowNonRestoredState`，也不改变 Kafka、Schema
Registry、Redis、Production、Staging 或 Shadow。

`monitor08`、`monitor09`、`monitor10` 的失败目录继续保留。完成代码 CI 和服务器部署后
必须使用新的运行编号重新投递合成事件，并在 revision、event_count、
changed_field_count、projection row 和 unique event count 全部一致后，才允许进入
10,000 条事件阶段。

## 2026-09-16：monitor21 受控 1002 条验收通过

PR #692 增加只允许当前 development run 与固定规模的受控验收入口；PR #694 修复
acceptance runner 只挂载新入口、却继续使用旧 worker 镜像内过期 runtime 和 Kafka
依赖的问题。修复后候选包把 runtime、投递台账、事件合同、信封与 relay 状态机模块
一并只读挂载。失败日志只允许输出异常类型和固定阶段，完整异常正文和业务值不会进入
Actions 日志；controller 会另写不可覆盖的私密失败证据。

本轮运行编号为 `dev-20260916-dualtrack-monitor21`，主线提交为
`6f8acd62383edc1c024ff1da013bbe69e34a09b8`。候选生成 workflow run 为
`35067807997`，部署 workflow run 为 `35067926982`，候选包 SHA-256 为
`f0bb9a6258f192d773f89708d09e88b83e5c4b1a88428b11f6c1c3552a7821c9`。
固定网关完成 `prepare → measure → apply` 后，持久 SSH MCP 只读复核确认
`current.json` 绑定相同 run、提交和四个不可变镜像摘要，且
`started=true`、`acceptance=pending`；Production 核心容器没有因本轮 Dev 部署重启。

1002 条受控验收 workflow run `35068247809` 通过，报告时间为
`2026-09-16T07:26:13Z`。投递台账为 1002/1002 published，Python 与 Flink 投影各
1002 行，Flink revision sink 为 1002 行，两边唯一事件均为 1002；
`revision_mismatch_count=0`、`projection_mismatch_count=0`、
`unattributed_difference_count=0`。受控成功报告保存在 Dev evidence 命名卷的
`/var/lib/binhu-dev-event-pipeline/evidence/scale-1002-*.json`，旧 monitor20 失败证据
继续保留，不覆盖。

随后第一次 10000 条 workflow run `35068567460` 在 15 分钟窗口结束时，投递台账已有
10000 条，但 relay 只发布 5351 条；Python 已处理 5351 条，Flink 暂时处理 5346 条，
`revision_mismatch_count=0`。这说明比较发生在消费者尚未全量收敛时，不能把窗口内的
5 条暂时落后写成最终计算语义差异。失败报告和暂停状态继续保存在 monitor21 的独立
evidence 目录，不覆盖。

relay 后续继续处理积压。同一 run 使用相同确定性 event ID 的幂等复核 workflow run
`35089738316` 在 24 秒内确认台账、Python 投影、Flink 投影、revision sink 和两边唯一
事件均为 10000；`projection_mismatch_count=0`、`revision_mismatch_count=0`、
`unattributed_difference_count=0`。因此 10000 条累计门禁已通过。首次超时的根因是
Dev relay 串行执行 claim、Kafka ACK、finish 和逐条日志，实测吞吐不足以支持后续
100000 条固定窗口；它不是最终双轨数据差异。

下一候选将 Dev relay 改为固定 8 个并发 worker、10 条派生库连接和共享的幂等 producer，
继续保留 `FOR UPDATE SKIP LOCKED`、lease token 与完成栅栏。并发度不能通过环境变量
任意放大，relay 仍只连接 Dev internal 网络，正常发布日志只写每 worker 每 1000 条的
安全计数。该变更必须使用新的不可变运行编号，从 1002、10000 再到 100000 逐级验证。

当前结果不表示连续 7 天/100000 条门禁、完整 Dev 第 6–11 项、checkpoint/savepoint
恢复或 Staging 晋级已经通过。旧 savepoint 的 operator ID 兼容恢复仍失败，当前继续
采用已记录的干净状态重建，不使用 `allowNonRestoredState`。

## 2026-09-17：monitor22 低并发锁竞争修复

`dev-20260916-dualtrack-monitor22` 的 1002 条验收在 relay claim 阶段触发真实 MySQL
1213 死锁。冲突对象为 `_kafka_event_delivery`，事务使用 `SELECT ... FOR UPDATE
SKIP LOCKED`；当时 relay 并发为 8，首批只有 7 条投递成功，relay 退出，剩余事件未
进入 Kafka。已处理的 7 条事件中 Python/Flink 的 revision、投影和计数完全一致，因而
本次失败归因于 relay 抢锁，而不是双轨计算语义差异。失败证据保留在：
`/data/docker/volumes/binhu-development-pipeline_evidence/_data/dev-20260916-dualtrack-monitor22/`。

修补合同固定为：relay 并发先降至 2、派生库连接池为 4；claim 和 finish 都把完整
事务作为重试单元，1213/1205 最多 4 次尝试，使用指数退避和随机抖动，超过上限进入
暂停状态并写入不含 SQL 参数、事件 ID 或业务正文的安全诊断；连接初始化使用
`READ COMMITTED`，不增大锁等待超时。每个 worker 保持独立 store 和重试上下文；一个
worker 暂停时等待其他 worker 收尾，再关闭 producer 和连接池，不能由单个异常取消
全部 worker。幂等 event ID、lease token 和 revision fence 保持不变。

下一轮必须使用全新的 `dev-20260917-dualtrack-monitor23`，从 1002 重新验收；只有
1002 零未归因差异后才进入 10000，随后才允许 100000。后续若 Dev relay 再次出现已
有幂等保护的瞬时锁竞争，可按同一边界降并发并新建运行编号；Production、Staging、
Shadow、数据一致性和安全边界仍是独立停止条件。

monitor23 的首轮证据必须同时记录 claim 重试次数与最终 relay 状态，避免把“已暂停”误判为规模通过。

## 2026-09-17：monitor23 10000 运行时失败归因

`dev-20260917-dualtrack-monitor23` 的 1002 条验收已通过：两边投影、revision、唯一
事件和未归因差异均为 1002/0。随后 10000 条验收失败，但不是锁重试耗尽或双轨差异。
服务器私有证据显示 relay 持续发布到两个 worker 各 5000 条；失败组件是常驻
`dual-track-monitor`，其容器反复因
`ImportError: cannot import name LockContentionExhausted` 退出。

根因是候选只把新的 `runtime.py` 挂载进 monitor 容器，而 monitor 继续使用旧镜像内的
`kafka_delivery_store.py`。`runtime.py` 顶层导入 relay 专用的新异常类，导致 monitor
在比较前无法启动。该失败目录和 `scale-10000` 报告保留不覆盖。修补方案是把该异常
改为 relay worker 内的延迟导入，使 monitor 启动不依赖 relay 新模块；下一轮使用
全新的 `dev-20260917-dualtrack-monitor24` 从 1002、10000、100000 重新验收。

monitor24 的部署摘要必须同时记录候选 runtime 与 monitor 所使用模块的兼容性检查结果。

## 2026-09-17：monitor24 高量级收敛窗口归因

`dev-20260917-dualtrack-monitor24` 已验证 monitor 模块兼容修复：1002 条验收中投递、
Python/Flink 投影、revision sink 和两边唯一事件均为 1002，差异计数为 0。10000 条
验收在旧 900 秒收敛窗口结束时只完成 4139 条发布，报告中的 3 条 projection 和 3 条
revision 差异来自 Flink 比 relay/Python 暂时落后，并非最终结果。验收结束后同一运行
编号继续收敛到投递、Python、Flink 和 revision 全部 10000，未发现 relay 退出、锁重试
耗尽或 Flink checkpoint 失败。

根因是固定验收窗口小于低并发 Dev relay 的实际高量级收敛时间，且旧报告把尚未完成的
计数缺口错误归入 `unattributed_difference_count`。验收合同现将“处理中”单独记录为
`convergence_pending_count`；只有全部计数达到目标后才判定投影或 revision 差异。
10000 和 100000 的等待窗口同时扩展，以覆盖受控低并发吞吐，仍保留固定上限，不无限
等待。monitor24 的失败目录保留不覆盖；修补部署必须使用新的运行编号，从 1002 重新
逐级验收。

验收器改动随 PR #704 提交，必须在主线 CI 通过后再部署。

## 2026-09-17：monitor25 逐级验收

由于 monitor24 的 10000 条失败证据必须保留，修补后使用新的运行编号
`dev-20260917-dualtrack-monitor25`。候选来自主线提交
`20ee8da047eaf3c2029e8141c303d8d665a38121`，固定网关更新 workflow 为
`35139743077`，候选部署 workflow 为 `35139922419`。

1002 条 workflow `35140088127` 已通过：delivery、Python/Flink projection、revision
和 unique event 均为 1002，`convergence_pending_count=0`，所有 mismatch 与未归因差异为 0。
10000 条 workflow `35140410050` 已通过：上述五类计数均为 10000，
`convergence_pending_count=0`、`projection_mismatch_count=0`、
`revision_mismatch_count=0`、`unattributed_difference_count=0`。两次证据均写入
monitor25 私有目录，未覆盖 monitor23/24。

100000 条 workflow `35142991989` 已启动，当前仍在运行；只有该级别完成且零未归因差异，
才可进入连续 7 天双轨计时和 Staging 晋级评估。

## 2026-09-17：monitor25 100000 入队故障诊断

`35142991989` 在入队阶段失败，安全分类为
`acceptance_failure_type=InterfaceError acceptance_failure_stage=enqueue`。私有证据目录
保留在服务器上的 `dev-20260917-dualtrack-monitor25` 下，未覆盖此前的 monitor23、
monitor24 或 monitor25 的 1002/10000 证据。失败时派生 MySQL 容器发生 cgroup OOM：
内核记录 `mysqld` 被 OOM killer 终止，容器以 137 退出后重新启动；relay 和 bridge
随后因数据库连接中断退出。Kafka、Flink、Schema Registry 没有运行时异常，失败不是双轨
结果差异。

重启前该运行编号已经提交了部分 100000 入队事务，数据库中保留了部分 pending/published
记录，因此该运行编号被标记为失败并永久停用，不能继续复用。修复仅限 Dev Compose 资源
门禁：派生 MySQL 的内存上限从 512 MiB 调整为 768 MiB，并显式设置 1536 MiB 的内存加
交换上限；生产、Staging、Shadow 和数据卷未修改。修复完成后必须使用新的运行编号，从
1002 → 10000 → 100000 重新验收。

## 2026-09-18：monitor32 100000 Flink 资源故障

`dev-20260918-dualtrack-monitor32` 来自主线提交
`67ab5078e789e469e430d767675a283bbfd6abb1`。1002 条 workflow `35299947299`
和 10000 条 workflow `35300140671` 均通过：delivery、Python/Flink projection、
revision sink 和两边唯一事件分别完整达到目标，projection/revision mismatch 与
未归因差异均为 0。

100000 条 workflow `35301406451` 在 GitHub Actions 六小时上限后被取消。取消前
100000 条事件已经全部发布，Python 投影已处理 100000 条；Flink 只处理 41571 条，
剩余 58429 条是未收敛缺口。服务器证据确认 Dev Flink TaskManager 在 768 MiB cgroup
上限内发生 OOM，并以 exit 137 退出；容器没有 restart policy，JobManager 随后因没有
可用 slot 持续处于 RESTARTING。Python metadata worker 也曾在 160 MiB 上限附近被
cgroup OOM 杀死一次，但依靠既有有限重启最终完成。两个 relay 正常，未发现新的
1213、1205、deadlock 或 lock wait。

本轮 `revision_mismatch_count=0`、`unattributed_difference_count=0`；现有证据表示
Flink 没有处理完全部事件，不能据此判为相同事件的业务计算差异，也不能把 100000
门禁标记为通过。失败证据保留在
`/data/docker/volumes/binhu-development-pipeline_evidence/_data/dev-20260918-dualtrack-monitor32/`，
不得覆盖或删除。

修补只作用于 Dev event-pipeline：TaskManager 提高到 2 GiB 容器上限与 1792 MiB
Flink process memory；JobManager 保持 768 MiB；二者使用 `on-failure:3`；Python
metadata worker 提高到 256 MiB。受控 Compose 合同只允许从历史精确配置迁移，保留
checkpoint/savepoint、Kafka、Redis、MySQL 数据卷和网络。修补合并后必须使用新的
运行编号，从 1002、10000、100000 逐级重新验收；100000 和连续 7 天门禁仍未通过。

`monitor33` 首次部署在 apply 的 Flink Compose 身份门禁停止。新候选正确要求 2 GiB、
1792 MiB 和 `on-failure:3`，服务器当前文件仍是更早的受控模型：JobManager/TaskManager
分别保留 1/3 slot 的历史属性文本、640 MiB process memory，且没有说明注释和 restart
policy。原迁移测试假设两边都是后来的 3-slot 带注释文本，因此拒绝了真实旧模型。
本次没有绕过门禁；`monitor33` 的 apply evidence 保留。迁移器补充的兼容范围固定为这份
完整历史文本，任意近似旧值或其他资源差异仍拒绝，修复后使用新的运行编号继续。

`monitor34` 的受控 Flink Compose 迁移已经成功：TaskManager 使用 2 GiB 容器上限、
1792 MiB process memory 和 `on-failure:3`，JobManager 使用 `on-failure:3`；二次
measure 证明不再需要配置变化，checkpoint/savepoint 卷、网络和数据卷均保留。
随后 apply 在首次读取 Flink REST 作业列表时失败。私有证据确认 JobManager 和
TaskManager 均正常启动、TaskManager 已注册、REST 稍后成功监听且没有 OOM；失败原因是
Compose 返回后 REST 尚未就绪，控制器第一次请求失败便立即退出，而不是运行时资源或
业务一致性故障。

`monitor34` 的失败证据和 apply evidence 必须保留且运行编号永久停用。控制器改为只对
固定的 REST 启动传输错误执行最多 60 秒的有界等待，其他错误以及后续 JobGraph、run_id、
consumer group、environment 和两个 sink 门禁保持不变。修复合并后使用全新的
`dev-20260918-dualtrack-monitor35`，从 1002 → 10000 → 100000 逐级重新验收。

`monitor35` 使用 REST 首次就绪修补后的候选。prepare、摘要校验、固定网关 prepare/measure、
Schema Registry、Compose 启动和 delivery index migration 均通过；控制器也成功越过首次
REST 就绪门禁、写出空的旧作业清单并提交当前 JobGraph。服务器私有证据确认该 JobGraph
最终为 RUNNING，两个 sink 的 4 个 task 全部启动，TaskManager 没有 OOM，Production
基线未受影响。

本轮 apply 仍失败于提交后的运行时收敛阶段。JobManager 初始化 JobGraph 期间，一次
`/jobs/overview` 达到 REST 客户端 15 秒上限；`wait_for_runtime()` 的 120 秒窗口没有捕获
这类固定传输错误，而是立即退出。该故障属于 Dev 验收工具竞态，不是业务双轨差异。
`monitor35` 的失败证据必须保留，运行编号不再复用。运行时等待修补后使用新的
`dev-20260918-dualtrack-monitor36` 从 1002 → 10000 → 100000 重新验收。

## 2026-09-18：monitor36 100000 入队容量门禁失败

`monitor36` 的 1002 和 10000 条累计验收均通过。100000 条运行在入队阶段失败，
不是 Python/Flink 结果差异：Dev 派生 MySQL 返回 `The table is full`，随后 relay
退出；失败时 `ibdata1` 使用 `innodb_data_file_path=ibdata1:12M:autoextend:max:1024M`
且 `innodb_file_per_table=OFF`。当时 `_kafka_event_delivery` 约 726 MiB（含索引），
Python/Flink 派生表仍需继续写入，因而 1 GiB 系统表空间在达到 100000 前耗尽。服务器
磁盘仍有充足空间，Production、Staging、Kafka、Flink 和 Schema Registry 未受影响。

本轮失败目录和 `monitor36` 1002/10000 证据保留不覆盖，运行编号永久停用。修补仅限
Dev event-pipeline：派生 MySQL 受控 Compose 将系统表空间上限从 1 GiB 提升到 4 GiB，
该上限仍由 Compose 合同测试和服务器 measure/apply 门禁固定校验；仍使用具名数据卷、
禁用 binlog、固定内存上限和有限资源门禁，不删除现有卷或验收数据。
修补合并并在服务器确认当前 Compose 只发生该预期变化后，必须使用新的运行编号从
1002 → 10000 → 100000 重新验收。

## 2026-09-19：monitor39 三档通过，monitor40 修复常驻观察

`dev-20260919-dualtrack-monitor39` 已完成三档规模验收。1002、10000 和 100000 的
delivery、Python/Flink projection、revision sink 与两边唯一事件均达到目标，
`projection_mismatch_count=0`、`revision_mismatch_count=0`、
`unattributed_difference_count=0`。100000 workflow `35380337042` 成功，证据保存在
`/data/docker/volumes/binhu-development-pipeline_evidence/_data/dev-20260919-dualtrack-monitor39/scale-100000-20260918T224241429979Z.json`。

三档通过不代表 7 天观察已经启动。常驻 monitor 随后因每轮完整载入两侧 100000 行，
在 128 MiB 上限内反复 OOM；规模 runner 又与常驻 monitor 共写 `status.json`，导致状态
文件可能显示运行中而容器已经退出。Dev 派生 Redis 的 48 MiB `maxmemory` 也出现 28 次
OOM，`bridge` 因 `noeviction` 写入失败退出。这些是 Dev 运行时与验收合同缺口，没有
发现双轨业务结果差异，Production、Staging、Shadow 未修改。

monitor40 修补固定为：健康轮次只在 MySQL 内完成计数和投影等值判断，差异详情最多
读取 100 条；常驻状态写 `monitor-status.json`，规模状态写 `scale-status.json`；Compose
healthcheck 要求当前 run ID、零未归因差异和 60 秒内心跳；规模 controller 必须等待
monitor 健康后才成功。Redis 使用 192 MiB 逻辑上限、256 MiB 容器上限和 384 MiB
内存加交换上限，保留 `noeviction` 和原数据卷。

修补经 PR CI、main CI 和固定 Dev 网关部署后，使用
`dev-20260919-dualtrack-monitor40` 依次重跑 1002、10000、100000。每档通过还需确认
monitor 为 healthy、`monitor-status.json` 持续刷新、bridge 正常、Redis OOM 计数无新增、
Flink checkpoint 无失败且 Kafka lag 收敛。100000 通过并观察至少两个心跳周期后，才
登记“7 天观察已正常启动”；连续运行满 7 天前不得登记为通过。checkpoint/savepoint
operator ID 兼容恢复仍是未通过的独立门禁。

## 2026-09-20：monitor40 三档通过与 6 小时强化观察

`dev-20260919-dualtrack-monitor40` 的 1002、10000、100000 三档均通过。100000 workflow
`35428539169` 在北京时间 2026-09-19 15:09 至 19:53 完成，最终
`delivery_published=100000`、Python/Flink projection、revision sink 和两边唯一事件均为
100000，projection/revision mismatch、convergence pending 和未归因差异均为 0。

项目管理人明确接受缩短长期观察，用 6 小时强化观察换取更快晋级，并把长期盲区单独登记。
观察从新的不可覆盖 `obs-YYYYMMDD-<name>` 编号开始，每 30 分钟采样一次，共 13 次。
开始时主动执行当前元数据域可安全手动触发的双轨对账、Schema 合同核对和状态汇总。
日报刷新和业务清理并未迁入该派生域，隔离 Dev Backend 也默认关闭业务调度器，因此本阶段
记录为当前域不适用；不通过 event-pipeline 网关越界调用生产或 Staging 业务任务。

核心阻塞项为：三档规模通过、6 小时所有采样健康、无未归因差异、无 OOM 且容器重启
计数不增加、Redis 驱逐和 OOM 计数无新增、MySQL 无当前锁等待或新增死锁、Kafka lag 收敛、Flink
checkpoint 推进、内存/连接/磁盘无持续增长趋势。证书续签、七天 Kafka 保留、Redis
自然 TTL、固定周期自然触发、跨天状态累积、长期内存和磁盘趋势明确列为未验证且不阻塞；
后续在 Staging 或生产灰度补充。
