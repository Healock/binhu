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
- Staging 副本仍未生成。早期排查曾记录多个固定原因码，其中部分来自临时诊断，
  不能全部当作未经修改的标准工具验收。当前可复核的标准工具失败见下方
  2026-09-11 数据门禁诊断记录。工具在门禁失败时没有写入候选库，
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

## 2026-09-11：数据门禁诊断与来源缺失

- 本次重新读取 Bootstrap：Production 为 `0.28.15`，Dev/Staging 仍为 `0.0.0`；
  环境身份分别正确。客户端构建和主线 CI 不能替代非生产运行版本更新。
- 当前 Dev 验证器返回最小事件流通过，MySQL/Redis revision 正确、3 条 published；
  `business_integration_verified=false`、`checkpoint_recovery_verified=false`。
  前面的历史恢复证据只覆盖对应元数据实验，不能签署完整业务恢复。
- `staging-1ac3a948054d4681`：修复提前扫描缺少诊断后，标准 measure 定位到
  `OnlineData.t_fullchain` 的核查结果字段同值命中。该字段已通过业务枚举校验。
- 按字段合同重新校验结果类别后，`staging-bbb872ad8fb9ea4c` 到达来源一致性检查，
  模型三业务表 261 条、当前来源 0 条。未写入候选库或切换 Staging。
- `staging-4e95b6b19439b90e` 增加只读分类统计：261 条全部有 active 登记，
  archived 登记 0 条，缺登记 0 条，重复业务键 0 条；65 条在归档中有同业务键。
  同业务键的旧归档不能证明当前 active 记录可以丢弃。
- 独立只读核对 `source-reconciliation-e60bbcad1d05a4f8`：当前本地和非本地来源
  均为 0；261 条业务值、业务键、登记内容和哈希全部一致，歧义 0 条。
  这是来源恢复方案的测量依据，尚未执行任何恢复。前面的三次诊断因脚本解析器
  标识错误失败，均保留独立失败记录，不能用这次成功结果覆盖。
- 工具包在 `environment-triad/snapshot-diagnostics-*` 独立目录留存，每包校验
  tar SHA-256 和逐文件 manifest，未覆盖早期代码或失败证据。

当前继续阻止完整副本导入；生产业务数据保持不变。Staging 来源恢复方式待确认，
Dev 业务闭环、同一制品晋级、浏览器回归和 75 人复测仍未完成。

## 2026-09-11：前端制品隔离修复准备

- PR #579 已合并为 `6ccc39c9`，PR CI `34518269688` 与主线 CI
  `34518902829` 均成功。该结果不代表 Staging 数据门禁通过。
- 只读检查实际 `/dev/login`、`/staging/login` HTML，启动脚本、JS、CSS
  均引用根路径，浏览器会从正式入口请求这些资源，无法支持环境间不同候选版本。
- 本分支新增 `environment` 构建模式，使用同一相对路径静态包，并由后端按
  固定环境插入 HTML base。旧根路径包在非生产环境返回结构化 503，不回退生产。
  更新必须同时切换 backend 与静态包，并保留配对回退材料。
- 此修复尚未部署。现有环境仍不能签署同一制品晋级与业务验收；不得把该修复
  或前端包构建成功当作 Staging 完整副本、Dev 业务闭环或压测通过。
- 浏览器验证发现发行说明还存在根路径兜底，已改为只请求当前环境固定路径；
  登录页新增环境和数据类型标题。前端 324 项、部署工具 153 项通过（2 项跳过），
  新 HTTP/静态资源测试 7 项在项目固定 FastAPI 版本的隔离 Python 环境通过。
- 本地 Chromium 使用同一静态目录、虚构 Bootstrap、无已登录用户，检查两环境的
  登录、深层任务入口和显式 index，共 24 个浅/深色桌面与窄屏场景。
  无跨环境资源/API请求，未出现横向溢出，并能加载实际 HelpCenter/DataQuery 分块。
  DPR 1.25 仅为浏览器模拟，不代替 Windows 系统缩放实机验收；不是内置浏览器
  对服务器的验收，也未验证已登录业务页面。
- 部分深层入口跳回登录时保留 `AbortError: Transition was skipped`（无 JS 栈）
  记录；这项页面过渡取消尚未解决，不能宣称所有浏览器交互无异常。
  本地证据为 `scratch/static-smoke-1789068748408`（浅色）及
  `scratch/static-smoke-1789068748473`（深色），早期失败截图保留在各自独立目录。

## 2026-09-11：Dev 候选版本与资源隔离已更新

- PR #580 的提交 `972bc054` 对应 CI `34523586318` 已全部通过；PR 仍保持 Open，
  本次按开发分支先进入 Dev 的流程更新，不发布生产或客户端版本。
- 从精确提交 `28d6c44ee0becf14f2eac34fdafacdcbec395bbe` 导出的源码和前端归档
  形成制品 `6d82514f8f938e87e6e2cf748574f6997393bbe6b1dd100047eda6943fcf4f76`。
  在服务器构建一次后端镜像，验证归档提交、版本、文件清单、镜像标签、APP_VERSION
  和镜像内全部应用文件；镜像 ID 为
  `sha256:1c6f31cc0c0fa586bf5b380f54ea191467368c29b6443c310666b86be35bff00`。
- 构建及测量证据在 `environment-triad/candidate-image-557deb2f31d64b94`。
  Dev 更新证据在 `environment-triad/dev-update-71048ce3b60cb032`，包含八库压缩
  备份及校验、原配置与静态资源摘要、候选配置、容器前后核对和成功报告。
- Dev Backend 已切换，MySQL、Redis、生产及其他既有容器的镜像、启动时间、
  运行和重启状态通过前后核对。Dev 八库身份再次通过；服务器本次测得可用内存
  约 4 GiB、Docker 所在磁盘约 247 GiB 空闲，Swap 接近用满，仍需持续监测。
- 公网 Bootstrap 实测：Production `0.28.15`、Dev `0.28.15`、Staging `0.0.0`。
  Dev 登录 HTML 已使用固定 `/dev/` base 与相对脚本 URL；此前“修复尚未部署”
  的记录现在仅适用于 Staging。Dev 身份为 development，API 入口为 `/dev/api`。
- 新增更新测试覆盖备份失败不切换、启动失败恢复配置、拒绝外部网络/数据库/挂载
  和环境覆盖。部署工具全集 171 项通过、2 项跳过、68 个子测试通过。
- 内置浏览器工具创建标签页超时，重新读取浏览器清单也返回连接错误。公网 HTTP
  核对通过不替代渲染、登录和业务页面验收；本次尚未取得内置浏览器验收证据。

这是 Dev 配套应用更新成功记录，不是完整业务验收。Staging 完整副本、Dev 业务
事件闭环、同一制品晋级、真实回退演练、业务回归与 75 人复测仍未完成。
