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
python -m migrations.photo_sheet_retirement migrate --apply
python -m migrations.photo_sheet_retirement verify
```

脚本在执行前要求照片工单数量为预期的 134 条，并检查共享附件引用；数量不符或发现共享引用会自动停止。执行前必须备份 `WorkflowData` 以及私人照片附件和 ZIP 目录，执行后保留 `measure`、`migrate` 和 `verify` 输出作为审计证据。

清理只针对照片调取专属记录和附件，不删除普通工单，也不触碰其他腾讯只读迁移快照、审计或回滚材料。`TXDOCS_MIGRATION_MODE=readonly` 的其他历史材料不受影响。

普通工单仍在“工单中心”中按原权限处理；照片调取不再是可用工单类型。
