---
slug: photo-workflow
title: 照片调取业务退役说明
category: 任务处理
summary: 照片调取业务已停止服务，本文说明历史数据清理边界。
order: 150
---
# 照片调取业务已停止

照片调取业务已经永久下线，平台不再创建、领取、导入、同步、重试或处理新的照片申请。旧客户端访问相关接口会收到明确的 `410` 响应。

## 历史数据

历史照片工单、导入批次、来源映射、outbox 和照片附件只通过维护窗口中的退役脚本处理：

```text
python -m migrations.photo_sheet_retirement measure
python -m migrations.photo_sheet_retirement migrate --apply --expect-photo-tickets <measure-count> --confirm-empty-photo-tables
python -m migrations.photo_sheet_retirement verify
```

生产维护必须通过“Production maintenance”固定工作流按 `measure → backup → apply → verify` 顺序执行。备份阶段同时保存 `WorkflowData`、照片附件和导入 ZIP；`apply` 必须使用同一运行编号实测的照片工单数量，并显式确认 `confirm-empty-photo-tables`。入口拒绝直接 SSH、任意 SQL 和跳过备份的清理。

`measure` 会核对数据库记录与附件、ZIP 文件的 SHA-256；执行 `migrate --apply` 时必须显式传入本次测量的照片工单数量，并确认九张照片专属表允许整表清空。数量不符、文件缺失、摘要不符或发现共享附件引用都会停止清理。执行后保留 `measure`、`migrate` 和 `verify` 输出作为审计证据；暂存目录清理失败会记录为待处理状态，`verify` 不会通过。出现待处理状态时先停止后续 apply，由维护负责人按受控流程核验暂存文件并处理，不得手工删除状态文件或绕过校验。

清理只针对照片调取专属记录和附件，不删除普通工单，也不触碰其他腾讯只读迁移快照、审计或回滚材料。这里删除的是照片调取业务记录，不是腾讯迁移材料；`TXDOCS_MIGRATION_MODE=readonly` 的其他历史材料不受影响。

普通工单仍在“工单中心”中按原权限处理；照片调取不再是可用工单类型。
