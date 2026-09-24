# 多环境版本与 PR 可追溯治理

- 当前状态：已确定方向，未开始实施
- 文档类型：多环境发布追踪与版本治理计划
- 计划边界：本文件只定义后续实现方案，不代表已经修改部署流程、服务器、容器或数据库
- 关联环境：生产环境、预发布环境、Dev 环境
- 目标：让项目管理人能够直接回答“每个环境当前运行什么提交、包含哪些 PR、比其他环境多了什么、哪些内容已经验收”

## 为什么需要这个项目

当前项目同时存在 `main`、`dev`、临时修复分支和紧急生产发布。PR 页面、Git 分支、CI、制品和服务器运行状态分别记录了一部分信息，缺少一个把它们绑定在一起的不可变发布记录，因此仅凭版本号无法可靠回答某个环境包含哪些 PR。

当前已暴露出三个具体问题：

- 生产环境可以因为一线功能需要直接推进，服务器实际提交可能领先于本地工作区或旧交接记录；这属于允许的业务决策，但必须留下精确来源。
- Dev 服务器可以停留在某个已部署提交，而 GitHub `dev` 分支继续合并新的 PR；“Dev 分支最新”和“Dev 服务器实际运行”需要同时展示。
- Staging 当前能够确认版本号和制品标识，但缺少可直接核对的 Git commit 与发布台账，无法准确列出其包含的 PR。

因此本项目管理的核心对象不是版本号，而是**环境发布快照**。版本号保留为人类可读信息，Git commit、制品摘要和部署记录负责提供可验证身份。

## 当前基线

以下是 2026-09-14 核查时的基线，只用于说明本项目要解决的追踪缺口；实施前必须重新读取实际环境和 GitHub 状态：

| 对象 | 可确认状态 | 当前缺口 |
| --- | --- | --- |
| 生产环境 | `0.28.22`，运行 `main@efbe243e`，对应成功的 Production 部署 Run `34801511547` | 需要把包含 PR 清单直接固化到发布台账 |
| 预发布环境 | `0.28.17`，容器运行中，有制品标识 | 容器和部署目录没有可确认的 Git commit，不能精确绑定 PR |
| Dev 服务器 | `0.28.20`，运行 `6d65c568`，对应 PR #623 | 服务器实际提交落后 GitHub `dev` 分支的 `7241d3fe`，对应 PR #626 |
| GitHub `main` | `efbe243e`，已包含 PR #638 | 分支状态不能代替生产实际部署状态 |
| GitHub `dev` | `7241d3fe`，包含 PR #626 的分支历史 | 分支状态不能代替 Dev 服务器实际部署状态 |

基线中的提交、版本和 PR 不是永久事实。后续报告必须以现场读取结果为准，历史基线只能作为审计背景。

## 目标模型

### 每次环境部署生成不可变发布清单

Production、Staging、Dev 使用同一种发布清单结构。一次成功部署至少保存以下字段：

```json
{
  "environment": "production",
  "version": "0.28.22",
  "commit": "efbe243e3ff499dff0743a4b611366f64ea94080",
  "source_branch": "main",
  "previous_commit": "367e1eee62d938f9326d75f83bc096bb35d24ad3",
  "included_prs": [
    {
      "number": 638,
      "title": "feat: add controlled dated current-flow cleanup",
      "merge_commit": "efbe243e3ff499dff0743a4b611366f64ea94080",
      "base_branch": "main"
    }
  ],
  "artifact_sha256": "<sha256>",
  "deployment_run_id": "34801511547",
  "deployed_at": "2026-09-14T03:14:12Z",
  "release_type": "normal",
  "status": "success"
}
```

`<sha256>` 只是字段示例，真正清单不得使用占位值。生产、预发布和 Dev 的实现应写入真实制品摘要。

字段含义固定如下：

| 字段 | 作用 |
| --- | --- |
| `environment` | 明确是 `production`、`staging` 还是 `development` |
| `version` | 展示用 SemVer，不作为唯一身份 |
| `commit` | 实际构建和部署的完整 Git SHA |
| `source_branch` | 选取源码的分支或来源说明 |
| `previous_commit` | 本环境上一次成功部署的 SHA，用于计算增量 |
| `included_prs` | 人可读的 PR 编号、标题、合并提交和目标分支 |
| `artifact_sha256` | 绑定实际传输或运行制品，避免同一版本号指向不同文件 |
| `deployment_run_id` | 绑定 GitHub Actions 或受控发布网关运行记录 |
| `deployed_at` | 使用 UTC 保存，界面按系统时区展示 |
| `release_type` | `normal`、`emergency`、`rollback` 或 `rebuild` |
| `status` | `started`、`success`、`failed`、`rolled_back` 等明确状态 |

### 发布清单和历史台账分开保存

每次成功、失败或回滚都要形成独立记录。失败记录不能覆盖上一次成功记录，回滚也要作为新的发布事件记录。

建议服务器目录结构如下，具体路径在实施前以现有发布网关为准：

```text
/var/lib/binhu-deploy/releases/<environment>/<version>-<commit>/manifest.json
/var/lib/binhu-deploy/history/<environment>.jsonl
/var/lib/binhu-deploy/current/<environment>.json
```

`current/<environment>.json` 只指向最后一次成功或明确回滚完成的发布。`history` 保存所有发布尝试的安全摘要。清单不记录密码、令牌、Cookie、手机号、身份证号、完整地址或业务正文。

### 环境必须暴露统一身份信息

容器标签、Bootstrap 或只读状态接口至少应能给出：

```text
APP_ENVIRONMENT
APP_VERSION
APP_COMMIT
RELEASE_MANIFEST_SHA256
```

推荐同时保留 OCI 标签：

```text
org.opencontainers.image.version
org.opencontainers.image.revision
org.opencontainers.image.source
```

其中 `APP_COMMIT` 与 OCI revision 必须相同；如果环境是从制品重建，仍然必须保留制品对应的源码 commit。无法提供这些字段的环境只能标记为“版本已知、来源未绑定”，不能标记为“可追溯”。

## PR 包含关系的计算规则

### 正常连续发布

当本环境从上一次成功提交连续推进到当前提交时，使用：

```text
previous_commit..commit
```

解析其中进入目标分支的 merge commit、squash merge 关联和 GitHub PR 元数据，并把结果写进发布清单。机器校验以提交祖先关系为准，人类查看以 `included_prs` 为准。

### 首次登记、重建和缺少上一提交

如果没有 `previous_commit`，不能伪造增量 PR。系统应执行以下规则：

1. 使用当前完整 commit 查询 Git 历史和 GitHub PR 关联。
2. 将结果标记为 `baseline`，表示这是首次登记，不代表所有历史 PR 都已逐一核对。
3. 保存制品摘要和登记时间，下一次部署从该快照开始计算增量。

### Dev 分支和 Dev 服务器分开比较

报告必须分别列出：

```text
GitHub dev branch commit
Dev server deployed commit
dev branch → Dev server 待部署 PR
```

不能用 `origin/dev` 的最新提交代替服务器版本，也不能用服务器容器版本代替分支最新状态。

### 紧急生产发布

紧急生产发布可以继续执行，但必须使用同一份清单格式，并将 `release_type` 设置为 `emergency`。至少记录：

- 紧急发布的精确 commit；
- 发布前生产 commit；
- 自动计算或经项目管理人确认的 PR 列表；
- 制品 SHA-256；
- 发布原因的安全摘要；
- 发布工作流 Run ID；
- 回滚 commit；
- 业务验收状态和后续补做的 Dev、Staging 验收。

紧急发布改变晋级顺序时，不改变“来源必须可追溯”的要求。

## 四种状态必须分开显示

每个 PR、每个环境和每次发布都要区分以下状态：

| 状态 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| 已合并到分支 | PR 已进入指定分支历史 | 不能证明任何环境已经部署 |
| CI/制品成功 | 指定 commit 通过对应检查并生成制品 | 不能证明服务器已运行 |
| 已部署环境 | 环境已经运行指定制品或 commit | 不能证明业务验收完成 |
| 已完成验收 | 指定范围的业务、页面或链路验收通过 | 不能推断其他环境也已部署 |

报告中禁止用“已上线”同时代替合并、构建、部署和验收四种状态。

## 面向项目管理人的报告

第一阶段先提供命令行或 CI 生成的 Markdown/JSON 报告，不急于建设复杂页面。报告默认输出当前摘要：

```text
环境          版本       实际提交       分支最新提交   部署状态   追踪状态
production    0.28.22    efbe243e       efbe243e       success      已绑定
staging       0.28.17    未记录         efbe243e       running      版本已知、来源未绑定
dev server    0.28.20    6d65c568       7241d3fe       running      服务器落后分支
```

报告还要按环境输出：

- 当前 commit、版本、制品摘要和部署时间；
- 上一次成功 commit；
- 当前发布增量包含的 PR；
- 相对目标分支领先或落后的 PR；
- 部署 Run 和验收记录链接；
- 无法确认的字段及其原因；
- 需要项目管理人处理的动作，例如“先补 Staging commit 绑定”或“Dev 服务器尚未部署 #626”。

默认不展开全部历史 PR，只显示当前发布增量和环境之间的差异；需要审计时再展开完整历史。

建议提供以下两个入口：

```text
python tools/environment_status.py
python tools/environment_status.py --environment production --history 10 --format json
```

命令的具体目录和运行时依赖在实施前按仓库现有工具布局确定。输出必须来自当前远端引用、发布清单和只读环境状态，不读取业务数据。

## 实施拆分

### 阶段一：统一数据模型和只读报告

- 新增发布清单 schema 与字段校验。
- 新增 PR 解析器，支持普通 merge、squash merge、同一 PR 多次检查和没有 merge commit 的 Open PR。
- 新增只读环境状态报告，明确区分服务器实际提交和远端分支提交。
- 对已有 Production `current.json` 做兼容读取，不覆盖历史台账。
- 为缺少 commit 的 Staging 输出明确的 `unbound` 状态。

阶段一完成后，可以先用报告看清环境差异，不要求立刻改变部署行为。

### 阶段二：接入部署网关和制品

- Production、Staging、Dev 的部署入口在部署前生成候选清单，在部署成功后原子写入成功清单。
- 在容器标签和 Bootstrap 中写入 `APP_COMMIT` 与 `RELEASE_MANIFEST_SHA256`。
- 保存制品 SHA-256、来源 commit、来源分支和部署 Run ID。
- 失败或回滚写入独立历史记录，不能覆盖成功指针。
- 确认部署网关不会把用户输入的任意地址、分支或 commit 当作未经校验的发布来源。

### 阶段三：补齐现有环境并建立门禁

- 为 Staging 重新登记当前实际运行制品的完整 commit，无法确认时保留 `unbound`，不能倒填猜测值。
- 为 Dev 服务器写入当前部署指针，并把 GitHub `dev` 分支最新状态单独展示。
- 检查历史生产部署是否能由 Run ID、commit 和制品摘要互相核对。
- 将“无发布清单”标记为追踪异常；允许紧急发布继续，但要求后补清单和原因记录。
- 在发布前检查 commit 是否属于预期来源分支或经批准的紧急来源，并检查目标环境身份。

### 阶段四：可视化和日常运维

- 将只读报告接入现有运维工作台或发布页面。
- 提供按环境、版本、commit、PR 编号和发布 Run 查询。
- 允许从环境卡片展开当前发布增量、历史发布和验收证据。
- 对追踪异常、环境漂移、分支领先服务器和 Staging 未绑定分别给出可执行处理路径。

## 文件范围建议

实施前必须重新核对实际目录和现有发布网关，以下是职责范围而不是已经确定的文件名：

| 组件 | 预期职责 |
| --- | --- |
| 发布清单 schema/模型 | 校验字段、状态和环境枚举 |
| PR 解析工具 | 从 commit 区间和 GitHub 元数据计算 PR |
| 环境状态命令 | 汇总发布清单、远端分支和只读容器身份 |
| Production 部署工作流 | 写入生产发布清单并保留历史 |
| Dev/Staging 部署入口 | 使用同一清单格式记录非生产环境 |
| 部署测试 | 检查身份、commit、制品摘要、失败保留和回滚记录 |
| 运维文档 | 记录正常、紧急、回滚和补登记流程 |

不得在未来计划阶段提前修改这些实现文件；开始实施时应另开实施 PR，并按照当前 `AGENTS.md` 重新确认分支、备份和真实环境边界。

## 验收标准

完成本项目后，至少满足以下条件：

- 任意环境都能从只读状态得到环境名、版本、完整 commit 和制品摘要。
- 任意一次成功部署都能从发布清单得到上一提交、当前提交、部署 Run、部署时间和包含 PR。
- 能分别回答“PR 是否合并”“制品是否成功”“是否部署到该环境”“是否完成验收”。
- 能准确显示 Production、Staging、Dev 服务器和 GitHub `dev` 分支之间的差异。
- Staging 不再只显示版本号；如果来源仍无法恢复，页面必须明确显示未绑定原因和补登记入口。
- Dev 分支推进但服务器未部署时，报告明确列出待部署 PR，不把分支状态说成服务器状态。
- 紧急生产发布可以记录为 `emergency`，并保留发布前提交、回滚提交、PR 清单和后续验收状态。
- 失败部署、回滚和重新构建不会覆盖此前成功发布的清单或证据。
- 发布清单、报告和审计摘要不包含凭据或真实业务正文。
- 自动化测试覆盖正常发布、首次登记、分支漂移、无 commit 的环境、紧急发布、回滚和失败保留；真实生产部署和真实 MySQL 验证分别记录，不能用静态测试代替。

## 停止条件和明确不做

出现以下情况时停止把环境标记为“可追溯”，保留当前证据并报告缺口：

- 运行中的制品无法还原到完整 Git commit；
- 发布清单与容器标签、Bootstrap 或部署网关的 commit 不一致；
- 无法确认制品 SHA-256 或部署 Run；
- PR 解析依赖猜测版本号，无法通过提交祖先关系或明确人工记录核对；
- 回滚覆盖了原成功清单，导致历史不可恢复；
- 环境身份、目标分支或部署来源校验失败。

本项目不做以下事情：

- 不因为追踪需要自动重启、重建或清理生产、预发布、Dev 容器；
- 不为了补齐版本记录向真实业务数据库写入测试数据；
- 不把 Staging 的版本号强行映射成未经验证的 Git commit；
- 不把 CI 通过、PR 合并或容器运行直接等同于业务验收；
- 不取消一线需求下的紧急生产发布，只要求紧急发布留下完整快照；
- 不恢复腾讯文档业务路径，也不在报告中输出敏感业务内容。

## 后续实施入口

开始实施前按以下顺序执行：

1. 重新读取 `AGENTS.md`、`docs/development-workflow.md`、`docs/operations.md` 和现有发布网关代码。
2. 现场读取 Production、Staging、Dev 的环境身份、版本、commit、制品和当前发布记录。
3. 先建立只读报告和 schema 测试，再接入部署写入。
4. 先在 Dev 验证报告与发布清单，再补 Staging 绑定，最后接入 Production 正常和紧急发布。
5. 每个阶段单独记录 CI、部署和验收结果；没有新证据时不得宣称三套环境已经统一。

## 2026-09-25：补齐 Dev 应用接受链

当前 Staging 应用晋级因缺少真实 `dev-update-*` 应用接受记录而阻塞。原有 Dev event-pipeline 接受记录不能替代应用接受记录。新增独立 Dev application gateway 和固定 workflow，职责与事件管线网关隔离，合同为 `prepare → measure → apply → accept`。

`0.30.24` 应用晋级使用精确 `origin/main` commit、artifact ID 和 Backend image digest。Dev 应用接受成功后，Staging 才能绑定同一制品执行应用晋级、受控数据库迁移和脱敏快照链。应用晋级链与 Kafka/Flink/Redis event-pipeline 验收链分开记录。

Staging 此前停留在 `0.28.17` 的原因是应用晋级 workflow 没有成功执行记录；此前安装的是 Staging promotion gateway 和脱敏快照 gateway，而不是一次成功的应用晋级。该流程缺口已通过独立 Dev 应用接受入口补齐。生产仍保持现有版本和运行路径，架构升级不包含在本次 `0.30.24` 应用晋级中。
