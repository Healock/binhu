# 生产、预发布与 Dev 环境建设

- 当前状态：架构调整中，尚未完成服务器验收
- 目标：生产保持正式业务；Dev 承载 Kafka、Redis、Flink 架构开发；Staging 承载脱敏回归和 75 人压测
- 已执行检查见[三环境运行验收台账](../plans/environment-triad-runtime-acceptance.md)；基础容器或最小事件流通过不代表全部验收完成。

## 职责

Dev 使用独立 Compose 项目、数据库、网络、卷、topic、Redis 和 Flink checkpoint，承载架构开发、小规模集成测试和开发者验收。现有 `binhu-kafka-shadow-*` 只能作为配置与链路基线，不能直接复用旧数据库卷或运行编号。

Staging 先按正式环境建立隔离副本，使用生产脱敏数据。只有 Dev 完成幂等、重试、checkpoint 恢复、重启恢复和 revision 条件写入验收后，候选版本才推进 Staging；75 人、5 分钟突发测试只在 Staging 执行。

历史 `binhu-loadtest-*` 属于旧压测运行资源，结果只作为趋势基线，不能直接改名为 Staging。

## 隔离要求

生产、Staging 和 Dev 使用独立 Cookie、数据库、Compose 项目、网络、卷、密钥、日志和资源上限。Staging 只能读取脱敏副本，Dev 只能读取虚构或已脱敏数据；两者禁止连接生产数据库、腾讯或其他生产外部写入服务。

## 推进顺序

1. 保存 shadow Kafka、旧 load-test 的配置、容器、网络、卷和运行证据。
2. 从 shadow 架构整理新的 `binhu-development` Compose，创建新卷和虚构数据。
3. 在 Dev 验证消息幂等、失败重试、checkpoint、重启恢复和 revision 条件写入。
4. 从正式环境生成并校验独立脱敏 Staging 数据库，创建 `binhu-staging` Compose。
5. 将 Dev 验证通过的版本推进 Staging，完成回归和 75 人压测。
6. 验证三套环境身份、Cookie、API、数据库、网络和账号隔离。
7. 新环境和压测替代能力验收后，才停用 `observer@shadow` 和清理旧 shadow 资源。

任何身份不一致、脱敏失败、跨环境访问、生产健康异常、资源不足或依赖关系不明，都必须停止清理和切换。

## 2026-09-30：三环境证据边界复核

本次只恢复和核对 Future Plans 文档，未连接三套环境、未修改容器或数据库。Dev 的派生域门禁记录、Staging 网关安装记录和 Production 的历史只读台账必须分别看待；基础容器启动、最小事件流、代码合入或 CI 通过都不能替代目标环境的业务验收。当前仍没有证据证明三套环境已统一到同一提交、制品或版本，也没有证据证明 Staging 75 人压测和回滚演练已完成。

后续任何晋级都要同时记录环境身份、完整 commit、制品摘要、部署 Run 和业务验收结果；缺少其中一项时保持 `unverified`，不得用版本号推断环境已部署。Production 继续使用本地 MySQL 业务表作为唯一主数据源，Dev/Staging 不得读取腾讯或其他生产外部写入服务。

