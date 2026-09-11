# 未来计划

这里记录尚未完成的长期项目及相关执行台账；具体是否已开始实施，以各项目状态和证据为准。总目录只提供项目索引；每项计划的范围、状态、验收和实施边界维护在对应项目文档中。

开始任何项目之前，必须重新核对 `AGENTS.md`、实际代码、当前需求、环境身份、数据范围和现有 PR 状态。计划中的内容不能当作已开发、已验证、已部署或已发布的事实。

## 项目索引

| 项目 | 当前状态 | 文档 |
| --- | --- | --- |
| 事件总线与实时计算基础设施 | dev 长期架构线承接；历史停止线及待验收项保留 | [事件总线与实时计算基础设施](future-plans/eventbus-realtime-infrastructure.md) |
| 腾讯表时代数据模型退场 | 长期迁移规划，实施前重新盘点 | [腾讯表时代数据模型退场](future-plans/legacy-task-model-retirement.md) |
| 生产接口拥堵诊断与抗压治理 | 排查记录已归档，整改另行推进 | [生产接口拥堵治理](future-plans/production-congestion-observability.md) |
| 外部出勤和请假系统对接 | 已确定方向，未开始 | [外部出勤和请假系统](future-plans/external-attendance-leave.md) |
| 流口标签与确认地址工作台 | PR/验收台账独立维护 | [流口标签与确认地址工作台](future-plans/flow-address-confirmation.md) |
| 地址匹配智能化升级 | 规划中，Dev 验证前未开始 | [地址匹配智能化升级](future-plans/address-matching-intelligence.md) |
| 生产、预发布与 Dev 环境建设 | 架构调整中，尚未完成服务器验收 | [环境建设](future-plans/environment-triad.md) |
| Dev 与 Staging 投入开发流程 | 规划中，待完成业务闭环、脱敏导入和验收 | [投入开发流程计划](future-plans/dev-staging-operational-readiness.md) |
| 场所码云端独立接收与本地主动拉取 | 待评审、待实施 | [场所码云端独立接收与本地主动拉取](future-plans/venue-code-cloud-ingress.md) |

## 目录规则

- 每个长期项目使用一个独立 Markdown 文件，文件名使用稳定英文 slug。
- 项目状态变化时先更新项目文档，再更新本页索引。
- 已完成项目应迁移到 `docs/plans/` 或链接明确的完成台账，不能继续伪装成未开始计划。
- 历史归档文件不回写；当前计划不得包含凭据、完整地址、身份证号、手机号或其他敏感正文。
- 生产、预发布和 Dev 的称呼和边界按项目文档及当前运行规则保持一致。
| 生产、预发布与 Dev 环境建设 | 架构调整中，尚未完成服务器验收 | [环境建设](future-plans/environment-triad.md) |\r\n
