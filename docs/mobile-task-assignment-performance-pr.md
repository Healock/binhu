# 组长分配核查人性能专项

## 改动范围

- `_online_source_projection` 增加来源/地址展示字段、规范化地址排序键和 `assignment_queue_ready`，并建立全局与社区复合索引。
- 启动时在 MySQL 命名锁下执行 500 条一批的可恢复投影回填，状态保存在 `_assignment_projection_backfill`；失败会阻止服务完成启动，完成后后续启动跳过。
- 分配工作台使用结构化投影字段，接口返回 `duration_ms` 与 `query_mode=indexed_projection`，不再读取完整 `values_json` 或执行 JSON 正则地址排序。
- 批量分配接口与前端分块上限调整为 100 条；成功任务在本地候选中即时移除，成功后只触发一次外层刷新。
- 候选列表使用 `@tanstack/react-virtual`，只渲染可视区域。

## 兼容性与边界

- 接口路径、权限、社区范围校验、来源版本校验、第一核查人责任固化和任务关系图语义保持不变。
- Redis 事件订阅、业务缓存、版本失效和实时局部同步不在本 PR 内。
- 不修改版本号，不包含真实业务数据、凭据或生产配置。

## 验证结果

- 后端：`1148 passed, 147 subtests passed, 1 skipped`；本机完整套件另有 2 个既有 Linux 资源换行检查失败（`desktop/server/tests/test_server_assets.py` 检查 `binhu-obtain-ip-certificate` 与 `nginx/binhu-updates-acme.inc`，本 PR 未修改这两个文件）。
- 前端：`293 passed`。
- 前端生产构建：成功（Vite 6.4.3，存在既有大包提示）。
- Python 编译：通过。
- `git diff --check`：通过。
- 2000 条 MySQL 同机房压力测试与 EXPLAIN 计划：当前开发环境未配置生产 MySQL，未执行；合并前需在测试库记录 p50、p95、最慢阶段，并确认新增索引被使用。

## 发布提示

首次启动会执行历史投影回填，时间取决于投影行数，建议发布备份范围为 `backup_scope=all / release_scope=auto`。发布前置 PR #479、#480 如已合并，应从最新 `main` 重新同步本分支并复核上下文。
