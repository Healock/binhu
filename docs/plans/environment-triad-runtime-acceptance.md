# 三环境建设运行验收台账

当前整体未完成；此文件记录已发生的检查，不能作为旧 shadow 退役或生产容量承诺。

## 2026-09-10：环境身份与 Dev 元数据链路

- PR #560 仅完成初版工具。其原始 shadow 字符串替换产物不应部署。
- PR #561（主线提交 `35698363`）修正为纯哈希证据提取，并补齐八库身份检查。
  Staging/Dev 分别补齐七个缺失身份表，verify 通过，再 apply 新增数量为零。
  两套非生产 backend 只更新了八库身份校验文件，不代表整个主线已部署到两环境。
- PR #562（主线提交 `7ca4c629`）PR CI `34422375432` 与主线 CI `34422822320`
  均成功。该批只增加 Dev 元数据事件工具，未部署生产或创建版本标签。
- Dev 运行编号 `dev-pipeline-20260910-a9409fce`：新项目
  `binhu-development-pipeline`，独立派生 MySQL/Redis 卷，连接现有 Dev eventbus
  内部网络，不连接生产或旧 shadow 网络，不复用其状态卷。
- 虚构事件 1/3/2/3 入队后，台账只有三个唯一事件，均为 published；Flink
  MAX(revision) 输出与 Redis 最新版本均为 3，最小事件流验证通过。
- TaskManager 重启后恢复检查点；恢复过程中记录了暂时无可用 slot 的错误，
  随后作业恢复 RUNNING 并继续完成检查点，不把这些暂时错误抹去。
- 保存 savepoint 后取消旧作业，新作业从该 savepoint 恢复；REST 返回
  `is_savepoint=true`，恢复路径与保存路径一致，随后七次检查点均完成。
  MySQL/Redis 仍为版本 3；relay 已独立重启。
- Flink JM/TM、relay、bridge 的 Docker 日志，以及 Flink 作业 plan/config
  扫描中未命中本次运行数据库和 Redis 密码。此结果只覆盖明确扫描的表面和时点。

## 发现的问题与证据范围

1. MySQL 首次初始化耗时超过 worker 的启动重试窗口；初始 worker 退出三次。
   等待数据库正式 TCP 就绪并验证身份后启动成功，后续代码加入健康检查依赖。
2. 新 checkpoint 卷默认属主为 root，Flink 无法创建 shared-state 目录。
   核对全部挂载引用后，只修改 Dev 卷根目录为 Flink 的 9999:9999，不递归修改状态。
3. 首次只读数据库就绪探测的原始输出被第二次成功探测覆盖。原始 worker 日志
   仍保留，具体连接拒绝诊断来自任务工具输出；另有明确标记的恢复诊断记录。
   不能将恢复记录冒充原始文件；后续探测改为唯一文件名，失败材料不得再覆盖。

服务器私密证据分别保存在 `environment-triad/identity-fix-35698363` 与
`environment-triad/dev-pipeline-a9409fce` 留存目录内，包括配置备份、镜像与依赖
哈希、失败/成功日志、检查点/恢复结果及生产容器基线。密码与旧私密配置不入 Git。

## 尚需完成的验收

## 2026-09-11：固定入口和账号隔离复核

- 通过生产服务器只读 HTTP 检查，`/staging/api/app/bootstrap` 返回
  `environment=staging`、`api_entry=/staging/api`、`environment_label=预发布环境 · 脱敏数据`；
  `/dev/api/app/bootstrap` 返回 `environment=development`、`api_entry=/dev/api`、
  `environment_label=Dev 环境 · 虚构数据`。
- `observer@staging` 使用服务器受保护初始化材料可登录 Staging，返回
  `binhu_staging_session`；同一账号访问 Production 和 Dev 均返回 401。
  `observer@dev` 可登录 Dev，返回 `binhu_dev_session`；同一账号访问 Production
  和 Staging 均返回 401。密码正文不写入台账。
- 当前服务器资源只读快照显示 Production、Staging、Dev 使用不同 Compose 项目、
  数据库命名空间和内部网络；Dev eventbus、Flink、pipeline 项目均已运行，
  但这不等于完整业务事件闭环和 Staging 可用副本已签署。
- Staging 副本仍未生成。近期独立失败证据只记录固定原因码：
  `source_sensitive_value_detected`、`unrecognized_business_date`、
  `unknown_enum`、`unsupported_current_source` 和
  `business_source_count_or_key_mismatch`。工具在这些门禁失败时没有写入候选库，
  没有切换 Staging 应用，也没有修改 Production。
- 因此固定入口和账号隔离已通过，但 Staging 脱敏副本、Dev 完整业务闭环、浏览器
  业务验收、75 人复测和 Shadow 退役仍不能签署。
- 两套现有环境的健康接口版本均为 `0.0.0`。复核发现环境准备器此前未把源代码
  `VERSION` 写入 `APP_VERSION`；已在本分支补上版本文件存在性、SemVer 校验、环境
  变量和 manifest 记录。现有运行实例未因该代码改动自动切换，必须在合并主线后以
  同一候选制品重新准备/更新环境，再复核版本身份。

PR #563 的 PR CI `34424666290` 和主线 CI `34425055670` 均成功，已合并到
`fc515d55`。部署前五个生产容器 ID、启动时间及重启次数与原基线一致。
新 worker 镜像已构建，但 Schema Registry 前置校验拒绝连接，尚未切换 worker。
实际只读检查表明运行的是 Apicurio 2.6.5.Final，其兼容接口在 8080 的
`/apis/ccompat/v7`，代码误用了 8081；现有实例还是无持久存储的 mem 镜像，
subject 列表为空。失败输出与原配置保存在独立 `dev-pipeline-3fdfb086` 证据目录。
PR #564 已合并到 `bec97ffb`，PR CI `34426137971` 和主线 CI `34426572521`
均成功。Dev Registry 已替换为官方 Quay 固定摘要的 Apicurio KafkaSQL 镜像，
仅替换 Registry 服务；Dev Kafka 三个 broker 的 ID 和启动时间均不变。
`dev.registry.storage.v1` 为单分区三副本 journal，min ISR=2，无时间/字节过期。
Schema ID=1、version=1，Registry 重启后的合同 SHA-256 与重启前一致。
新 worker 已部署，重复消息重放后 MySQL/Redis revision 仍为 3、投递台账仍为
三个 published；现有 Flink 作业累计完成 264 次检查点、零失败（该检查时点）。
Registry 重启初始化期间的连接失败，以及 worker 重建期间提前验证返回 137
均保留在独立证据文件，随后就绪验证成功，不记作首次即成功。
五个生产容器 ID、启动时间和重启次数与原基线相同，生产 Bootstrap 正常。
数据库/Redis 密码未命中本次检查的 worker/Flink 日志与作业 plan/config。
证据目录为 `environment-triad/dev-pipeline-79edefbf`；Flink Java 作业仍是原
`a9409fce` 构建，尚未把后续语句编号诊断代码切入当前运行作业。

- Schema Registry 合同注册、启动一致性和重启持久化已验证；后续持续记录版本变化。
- 业务派生计算、Backend/实时查询、WebSocket、完整故障与消息重放验收。
  当前元数据聚合不能替代地址匹配、人员标签或任务图业务计算。
- 真正的 Staging 白名单生产脱敏副本、引用关系报告、零敏感值扫描及失败回退。
  现有 `staging_snapshot.py` JSONL 工具不足以签署这些检查。
- 内置浏览器首次改密与业务页面验收。HTTP 登录通过不等同页面体验已验收。
- Staging 新运行编号下的 75 人/5 分钟复测及清理。
- 完成替代能力后才核对旧 shadow 的监控、回滚和运行依赖，停用账号/入口并清理资源。

## Staging 白名单准备工具（尚未导入）

新增 `deploy/environments/staging_data/` 的只读 `measure`/`export` 入口：
一次一致性事务读取当前任务、组织和地址图，生成独立假主键和假名，检查任务与
来源集合、引用关系及嵌套 JSON 敏感值。历史 revision 和哈希一致/过期语义保留。
密码、会话、附件及未知配置不导出；每次独立编号，失败证据不覆盖。

本地部署工具测试 114 项运行，112 项通过、2 项按既有条件跳过；Python 编译通过。
当前尚无服务器真实 MySQL 导出验证或 Staging 导入。工具固定输出
`ready_for_application_switch=false`，等待历史摘要、任务地址确认、登记 HMAC、
候选八库导入和目标验证补齐，不以纯测试签署数据可用。
