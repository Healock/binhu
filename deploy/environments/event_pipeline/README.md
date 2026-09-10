# Dev 任务元数据事件链路

此目录承接旧 shadow relay 的任务元数据合同、独立投递台账和 Redis 版本校验，
不承接照片写回、场所同步、业务正文或旧运行数据。生产 Redis relay 不受此目录影响。
原模块从已留存的旧运行镜像源码提取；配置、凭据和状态卷未进入 Git。

环境固定为 `development`，运行编号为新的 `dev-*`。Kafka topic 固定为
`dev.task.events.v1` 和 `dev.task.events.dlq.v1`，MySQL 固定为独立
`Dev_EventPipeline`。数据库与 Redis 使用新卷，资源项目为
`binhu-development-pipeline`，只加入已验证的 Dev eventbus 内部网络。
该实验不接入生产或 Staging，也不修改平台业务 Backend 的网络。

## 操作顺序

1. 合并与主线 CI 通过后，从已核对含 aiomysql、aiokafka、redis 的本地镜像
   构建本目录 Dockerfile，记录基底与结果镜像 ID。镜像安装依赖需另外固定版本，
   不允许运行时从互联网安装。
2. 在服务器执行 `python -m event_pipeline.prepare --run-id <新的编号>
   --mysql-image <sha256镜像ID> --redis-image <sha256镜像ID>
   --worker-image <sha256镜像ID>`。仅生成新目录；拒绝已有目录、卷与项目。
3. 执行 `python -m event_pipeline.control measure`，核对清单、网络、卷引用及内存；
   用准备好的 Compose 执行 `run --rm --no-deps relay python -m
   event_pipeline.schema_registry apply` 注册固定合同，再执行 control 的 `apply`。
   启动前会再次检查合同。失败证据保留，不能覆盖旧输出后宣称首次成功。
   新 MySQL 初始化可能超过两分钟；relay/bridge 必须等待 TCP 健康检查通过，
   不能把容器启动或只开放临时 Unix socket 当作数据库就绪。
4. 为 Dev Flink 准备 Kafka 3.3.0-1.20、JDBC 3.3.0-1.20、MySQL Connector/J 8.4.0
   依赖，下载校验与许可证记录保留在外部证据目录。Flink 为 1.20.1、Java 17。
   编译 `PipelineJob.java`，将私密 `pipeline.sql` 只读挂入
   `/opt/flink/private/pipeline.sql`，设置 `APP_ENVIRONMENT=development`。
   不使用会回显 SQL 和密码的交互 SQL Client；提交 Java 入口，并检查日志无凭据。
5. JobManager 与 TaskManager 使用 Dev 自己的 checkpoint 卷和内部网络。
   提交前执行 `python -m event_pipeline.checkpoint measure`，如新卷根目录属主
   不匹配，再 `apply`；工具核对全部运行/停止容器的卷引用，只调整卷根目录，
   不递归改写旧检查点。新 Docker 卷默认属于 root，不能假定 Flink 用户可写。
   注册 schema、确认作业为 RUNNING 后执行 `event_pipeline.verify seed`，
   再执行 `event_pipeline.verify verify`。首次验收包含重复入队、乱序 revision、
   Kafka ACK 后台账完成及 MySQL/Redis 最终 revision 一致。
6. 另行记录 checkpoint/savepoint 创建、TaskManager 恢复、relay 重启和重放结果；
   `verify` 只报告最小事件流，不会替这些步骤或业务集成签署通过。

Schema Registry 使用固定内部服务与 `dev.task.events.v1-value` subject。
先以新 worker 镜像执行 `python -m event_pipeline.schema_registry apply`，再启动
新 relay/bridge；启动会检查 Registry 的 JSON Schema 与当前代码完全相同，
身份不匹配或重定向即拒绝。topic 仍使用原始 JSON，不采用 Confluent 二进制 framing；
Registry 绑定合同版本，发送前 Python 验证器执行完整字段和时间/整数检查。

## 凭据、资源及回退

派生库和 Redis 密码独立生成，仅保存服务器私密目录；不传入 CLI 参数或普通日志。
目录权限 0700；供容器服务用户读取的 SQL/Redis 单文件位于此保护目录中。
投递错误使用固定类别，不输出异常正文。Schema/查询和作业计划也应扫描凭据，
发现明文进入普通日志应立即停止运行，保留私密失败证据并修复。

MySQL 512 MiB、Redis 96 MiB、两个 worker 各 160 MiB 内存上限；CPU 分别
0.5、0.25、0.25、0.25。MySQL 用户数据系统表空间上限 1 GiB、禁用 binlog；
Redis 内存 48 MiB，禁止自动淘汰，AOF 有重写阈值。Docker 日志限制 5 MiB × 2。
宿主剩余磁盘仍需监控，不能把表空间上限当作整个文件系统配额。

回退只停止本项目 worker 和作业，保留独立卷、配置、镜像与 checkpoint。
不执行 `down -v`，不删除 shadow，不重启生产。Schema Registry、业务派生计算、
Backend 读取与 WebSocket 的完整验收需单独证据；此元数据 MAX(revision)
作业只能证明传输、聚合和版本保护基础能力，不能冒充业务架构已经全部迁移。
