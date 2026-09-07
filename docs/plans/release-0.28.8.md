# 0.28.8 生产小版本执行台账

最后更新：2026-09-07。未勾选项不能视为完成。

## 范围与分支

- 生产稳定线：`main`，发布分支：`release/v0.28.8`。
- 长期 Kafka/Flink/Redis 继续留在 `dev`，本版本不启用。
- XLSX 清理和导入等待文件及维护窗口，本次不执行。
- 影子运行：`rls-20260907-c`；证据留存于服务器 `/srv/binhu-release-evidence-rls-20260907-c`，不随项目清理。

## 实施与验收台账

| 顺序 | 工作 | 状态 | 证据/下一步 |
|---|---|---|---|
| 1 | 分支和 0.28.8 版本范围 | 已完成 | 提交 `1d93ed21`，合并提交 `e7a437612455ae71387bff02f5c2bdf4cca300ba` |
| 2 | 管家码产生指令纳入地址待变更 | 已完成 | 后端回归与 PR CI |
| 3 | 操作后增量在线汇总 | 影子已通过 | `summary-verify.json`：同日/跨日上限、revision、首核查人、公开读取、事务回滚 |
| 4 | 操作记录 SSE | 影子已通过 | `realtime-attempt-4.json`：权限、脱敏、心跳、Last-Event-ID、会话失效 |
| 5 | 在线查询 WebSocket | 影子已通过 | `realtime-attempt-4.json`：保存、revision 冲突、普通岗位拒绝 |
| 6 | 独立确认地址页面 | 本地 Chrome 已通过 | 1280/1680/1920 桌面与 390 窄屏，行间距 12px，无横向溢出 |
| 7 | 文档、帮助、架构、运维 | 已完成 | 同一提交更新 |
| 8 | 全量检查 | 已完成 | 后端 1182 passed/141 subtests；前端 315 passed；构建、compileall、diff-check 通过 |
| 9 | PR、合并 | 已完成 | PR #514，CI 全部通过，已合并 |
| 10 | main CI、生产部署、标签 | 待执行 | main CI 通过后，用固定 Deploy production；上线只读验收后再创建 `v0.28.8` |

## 发布门禁

- [ ] main CI 通过。
- [ ] 固定工作流使用精确合并 SHA、`expected_version=0.28.8`、`backup_scope=all`、`release_scope=full`。
- [ ] 生产八库与旧程序备份成功且可核验。
- [ ] Nginx WebSocket Upgrade、SSE 无缓冲配置检查通过。
- [ ] 健康版本、容器、日志、汇总队列和关键页面只读验收通过。
- [ ] 回退保留兼容表和已提交业务数据。
- [ ] 验收通过后创建 `v0.28.8`，再以独立提交同步生产修复到 `dev`。

## 恢复入口

读取本台账、`AGENTS.md`、工作区状态和 main CI 状态，从第 10 项继续。禁止删除独立证据目录；XLSX 仍等待用户提供。
