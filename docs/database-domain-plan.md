# 八库业务域与新档案规划

本文描述按业务域拆分数据库和新增辖区档案、人员标签、通用工单的长期结构。迁移采用维护窗口分阶段进行，旧表在核对完成后仍保留只读回退材料，不进行长期双写。

## 数据库边界

平台继续使用同一个 MySQL 实例，按以下八个 schema 管理：

| 数据库 | 责任范围 |
| --- | --- |
| `PlatformData` | 用户、人员、部门、社区、权限、会话、通知、审计、出勤、排班和个人实际工作事件 |
| `OnlineData` | 腾讯配置、同步、来源行、来源投影、在线业务表和回写记录 |
| `OnlineDataArchive` | 在线表移除后的只读归档 |
| `daily_report` | 每日快照、任务流水、日报和工作日志草稿 |
| `VisitData` | 走访导入批次、明细和异常 |
| `DispatchData` | 全链条下发批次、审核任务和发布结果 |
| `RegistryData` | 辖区房屋、地址、房屋相关人员、机构、人员标签和标签快照 |
| `WorkflowData` | 工单类型、流程版本、节点、工单、评论、附件元数据和事件流水 |

数据库之间不建立跨库外键，只保存稳定 ID、业务类型和来源键，由后端在同一 MySQL 事务中校验关联。

## RegistryData

房屋使用稳定 ID，标准识别维度为“街道/辖区—小区或自然地址—幢—室”，并保留地址版本和别名。房屋子单元表已预留，但第一版不实现合租房间业务。

房屋相关人员与平台工作人员、重点人员档案互相独立。同一人可以管理多套房，同一房可以有多个角色和多个管理人。角色字典预置业主、房东、二房东、中介经办人、租房平台联系人、实际管理人和物业管理人，角色记录保存生效及结束时间。

身份证号和手机号按项目管理人确认采用明文保存，同时保存 HMAC-SHA-256 摘要用于精确查询、去重和任务命中。HMAC 密钥来自服务器私密配置，不写入数据库；摘要保存密钥版本。普通日志、审计和通知不得出现完整敏感字段。

指令核查只按身份证 HMAC 精确匹配人员标签。标签只负责醒目展示和筛选，不改变任务完成口径、社区分配或默认优先级。任务首次下发时保存标签快照；后来补录过去生效的标签时按有效日期回填历史任务并留下补录原因，解除当前标签不会删除历史快照。

## WorkflowData

工单采用通用核心表加类型扩展表。流程版本发布后不可修改，工单固定使用创建时的流程版本。第一版以顺序审批为主，表结构预留任一通过/全部通过会签。

照片调取申请类型默认启用，全部业务岗位可发起，基础管控进入处理队列并由处理人领取。请假类型保留但初始不发布，由超级管理员配置审批链后启用。管理员直接登记请假继续兼容，工单审批通过时以 `workflow` 来源写入出勤历史，撤回时停用对应记录。

附件保存于受保护目录或对象存储，MySQL 只保存随机文件 ID、哈希、大小、密级、上传人和保留期限。敏感附件默认结单后保留 90 天，文件删除后保留元数据和删除事件。

## 迁移顺序

1. 先测量表大小、索引大小和关键查询执行计划。
2. 创建 `RegistryData`、`WorkflowData` 及其基础表，新业务直接使用新库。
3. 在维护窗口迁移走访到 `VisitData`、下发到 `DispatchData`，逐表比较行数和关键摘要。
4. 再迁移用户、人员、组织和系统配置到 `PlatformData`。
5. 最后让 `OnlineData` 只保留腾讯在线来源和业务表。

每个阶段都停止相关写入、按批复制、比较结果、切换连接并保留原表只读备份。不同库不长期双写。

## 索引规则

新增索引必须绑定具体查询并提供执行计划对比。优先评估：

- 在线投影：`parser_type + community + inspector + task_state`、`parser_type + task_state + updated_at`、`parser_type + identity_hmac`。
- 日报流水：`parser_type + report_date + task_state`、`report_date + community + inspector`。
- 档案：身份证 HMAC、手机号 HMAC、标准化地址、社区和启用状态、标记有效期。
- 工单：队列/状态/截止时间、发起人/时间、处理人/状态、对象关联和事件时间。

`LIKE '%关键词%'` 不依赖普通 B-Tree，后续根据真实查询量再选择精确索引、前缀索引或全文搜索。

## 当前状态

- 2026-08-08 已完成第一阶段走访域迁移：`_visit_import_batches`（35 行）、`t_visit_details`（10,678 行）和 `_visit_import_issues`（299 行）已从 `OnlineData` 复制到 `VisitData`。
- 三张表的字段结构、行数和主键边界均已通过真实 MySQL `verify --domain visit` 核验；`BINHU_VISIT_DOMAIN_ACTIVE=true` 已切换到新库，旧表保留。
- 第二阶段已把空的下发三表迁入 `DispatchData`，并把小区地址 93 条、地址来源 93 条、历史导入 2 条和冲突 0 条迁入 `RegistryData`；两个域均通过结构、数量、主键边界和索引对比。
- `DISPATCH_DOMAIN_ACTIVE=true` 和 `REGISTRY_ADDRESS_DOMAIN_ACTIVE=true` 已切换；迁移前后八库备份均已完成并校验。Registry/Workflow 功能及平台、日报业务域仍未切换。
- 第三阶段已完成 `PlatformData` 迁移：24 张平台基础表已从 `OnlineData` 复制并通过结构、行数、主键边界和关键索引核验；用户 75、人员 72、用户权限组关系 3 条与源库一致。
- `BINHU_PLATFORM_DOMAIN_ACTIVE=true` 已切换并重建后端与运维代理；同步任务已成功完成 12/12 步，近期后端和运维代理错误日志无新增错误。
- `_sync_schedule` 与 `_sync_log` 按当前固定路由继续保留在 `OnlineData`，没有纳入本阶段 `PlatformData` 迁移；这不是漏迁，后续仍以代码白名单和真实同步验收为准。
- 第三阶段迁移前备份为 `/backup/binhu/migration/0.16.0-platform-pre-migration-20260808T155700Z.sql.gz`（SHA-256：`8c3db54c52efc07192ef345ea7eaac00578aa7f04d8cfeae21f0007f772aee6a`），迁移后备份为 `/backup/binhu/migration/0.16.0-platform-migrated-20260808T160239Z.sql.gz`（SHA-256：`98e9c457df9c957354fa9af619baeddab923b40ca3625014fad178e7730636e2`）。
- 第四阶段已完成日报域中的工作日志草稿迁移：`_work_log_drafts` 3 条从 `OnlineData` 复制到 `daily_report`，结构、行数和主键边界一致；`BINHU_DAILY_DOMAIN_ACTIVE=true` 已切换。
- 第四阶段迁移前备份为 `/backup/binhu/migration/0.16.0-worklogs-pre-migration-20260808T163351Z.sql.gz`（SHA-256：`2664fbf2d677914b44e7585a79bc66b709286264a8353a3630ccaa00ec8990b4`），迁移后备份为 `/backup/binhu/migration/0.16.0-worklogs-migrated-20260808T163528Z.sql.gz`（SHA-256：`6f36ed8dbaa9a6f5c361103e79127411f86d3454e3e68468a0a81f54b18822c9`）。
- 第五阶段已完成身份证 HMAC 回填：生产服务器生成专用 HMAC v1 密钥并保存到 root-only 私密配置；在线投影 673 条中 339 条有身份证字段，全部重算并核对一致，334 条无身份证字段继续为空。
- `RegistryData` 当前辖区人员、人员标签分配和任务标签快照均为 0 条，因此本阶段没有可回填的历史快照，不向业务表制造测试数据。
- 第五阶段迁移后八库备份为 `/backup/binhu/migration/0.16.0-hmac-backfilled-20260808T174803Z.sql.gz`（SHA-256：`b283849786e3837ae611747afa647d0025c6b1ad776433b4fb6368b8888549a6`）；HMAC 私密配置备份不进入数据库备份或 Git。

- 八个数据库名称、连接池、固定表白名单路由和初始化 schema 已加入代码。
- `domain_migration` 默认只读，复合主键表不会按单一主键错误分页；旧表和旧数据库暂不清理。
- 真实 MySQL 的走访、下发、小区地址、平台基础、工作日志和身份证 HMAC 域测量、复制、逐表核对、备份及生产切换已完成；人员标签快照因当前没有标签分配而无可回填记录。
- `RegistryData`、`WorkflowData` 功能开关在迁移完成前保持关闭，避免新页面在空库上产生业务写入。

- 新数据库名称已经加入配置和 Compose。
- 新数据库缺失时，旧三库平台会记录提示并继续启动，方便分阶段迁移。
- `RegistryData`、`WorkflowData` 基础表由后端启动兼容初始化。
- 生产迁移已完成走访、下发、小区地址、平台基础、工作日志和 HMAC 回填六个阶段；旧表和各阶段迁移前后八库备份继续保留，尚未执行删除或恢复操作。
