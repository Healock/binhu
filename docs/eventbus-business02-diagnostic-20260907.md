# business02 预检停止记录（2026-09-07）

目标仍是完成 Outbox 全量接入、Flink/Redis 真实业务双轨和 75 人 5 分钟复测。本记录不代表最终验收通过。

项目目录 `/srv/binhu-eventbus-shadow-20260907-business02`，Compose 项目 `binhu-kafka-shadow-20260907-business02`，运行号 `KSHADOW-20260907-business02`。仅在该隔离项目变更，未修改生产或既有 `binhu-loadtest-lt-*`。

## 已取得证据

- Seeder 退出码为 0，76 用户、3600 任务、48 房屋属性，六类业务各 600 条。证据：`artifacts/business-seed-fixed/seed.log`、`exit.code`。
- 初始主题缺失使两条事件进入 DLQ；建主题后 Relay 继续投递。2026-09-06 23:06:55 UTC 的数据库统计：3598 published、2 dead_letter，无 pending/retry/publishing。此为 ledger 排空证据，尚未独立核对 3600 条 Kafka 消费结果。
- `artifacts/boundary-audit-current/result.json` 保存统计、影子数据库 marker、资源及边界检查。磁盘充足，可用内存约 5.6 GiB，但 Swap 使用接近上限，压测前仍须重新评估资源。
- 本轮任务主题为三分区、RF2、retention.ms=604800000、min.insync.replicas=2；不等于自然经过七天删除已验证。

## 问题和修补

复制目录时遗留 business01 的绝对 schema 只读挂载与旧 deployment manifest。已核对两份 init.sql 哈希一致，将挂载改为 business02 内文件，保存旧 manifest 并重新生成本项目身份清单。只重建本项目 MySQL 容器，已有数据卷保留；初始化 SQL 不重跑。证据在 `artifacts/boundary-repair-01/`。

随后完整预检依次失败：

1. `preflight-repaired.log`：检查器只允许 `-network`/`_network` 名称，不接受当前项目 `_default` 网络。新增精确项目名后缀校验及跨项目拒绝测试，本地 30 项通过；没有放宽 internal/容器成员/项目标签验证。
2. `preflight-repaired2.log`：MySQL 重建期间 Relay 连续数据库连接失败退出。确认退出原因后，仅重启 business02 Relay。
3. `preflight-repaired3.log`：Kafka 容器缺少检查器要求的 `binhu.shadow.scope=business` 标签。预检仍拒绝，未运行压测。

按用户“三次修补后同一项仍失败即等待人工”停止服务器变更和压测启动。不能忽略 scope 检查、伪造通过 manifest 或继续重试。未停止业务真相数据库、未删除项目数据、未关闭旧 Python worker。

## 人工介入后的恢复顺序

1. 先复核上述三轮日志和实际 Compose/容器标签，决定统一业务 overlay 标签还是建立明确的分服务 scope 合同；不得直接放行任意标签。
2. 完整预检通过后才继续事件消费核对；保留 DLQ 历史，通过可追踪回放验证恢复，不直接改写 ledger 成功状态。
3. 验证部署镜像包含当前提交，当前部分事务接线仅在本地；独立 Runner 仍有部署可达性及运行索引缺口，不能将其七项模拟测试说成可运行验收。
4. 补齐辅助 Outbox 业务接线、原始输入回读、四类 Flink 真实派生与 Redis/MySQL 独立输出。现有投影回读接口包含 Python worker 结果，不得用于自证。
5. business02 只用于调试。最终复测另建全新卷，先完成身份检查、主题和 Schema 初始化，再启动 Relay/Backend 和 Seeder，避免重复本轮初始化顺序错误。
6. 执行 75 用户 300 秒突发并保留接口、锁、lag、checkpoint、Redis 和停止写入后排空指标；连续 7 天/10 万事件是关闭旧 Worker 的独立门槛，不得以五分钟复测替代。

本地代码基线：`be70e8da` Seeder 任务事件、`9d0e6ac4` 身份别名检查；后续提交以 Git 为准。所有线上原始日志在该项目的 `artifacts/`，凭据不进入文档。
