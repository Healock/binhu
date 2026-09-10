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

- Schema Registry 合同注册和启动一致性校验的实际运行。
- 业务派生计算、Backend/实时查询、WebSocket、完整故障与消息重放验收。
  当前元数据聚合不能替代地址匹配、人员标签或任务图业务计算。
- 真正的 Staging 白名单生产脱敏副本、引用关系报告、零敏感值扫描及失败回退。
  现有 `staging_snapshot.py` JSONL 工具不足以签署这些检查。
- 内置浏览器首次改密与业务页面验收。HTTP 登录通过不等同页面体验已验收。
- Staging 新运行编号下的 75 人/5 分钟复测及清理。
- 完成替代能力后才核对旧 shadow 的监控、回滚和运行依赖，停用账号/入口并清理资源。
