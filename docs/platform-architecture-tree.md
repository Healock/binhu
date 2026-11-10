# 滨湖智慧平台架构树与重构台账

> 状态基线：2026-11-06。本文是业务与技术模块的架构索引，不是源码目录树。功能存在不等于已经迁入目标架构；只有标记为“已验收”的节点才算完成对应重构项。

## 状态标记

- `[现行]`：当前平台已有该业务能力；不表示它已经使用 Kafka/Flink/Redis。
- `[部分]`：目标架构只完成了合同、局部接线或部分链路。
- `[源码未收口]`：已有目标架构代码，但仍有明确源码路径未覆盖。
- `[未完成]`：目标能力或盘点工作尚未完成。
- `[已验收]`：代码和对应环境的业务链路证据均已核实。
- `[待做]`：目标架构尚未实现。
- `[待盘点]`：平台能力存在，但尚未逐项做新架构评估。
- `[下线]`：旧功能已停用，只保留兼容或历史材料。

## 架构分层概览

```text
滨湖智慧平台
├── 1. 客户端与交互层
│   ├── Web：React + TypeScript + Vite
│   ├── 手机业务工作台：指令核查、研判、分配与任务处理
│   ├── Windows 客户端：Tauri / Electron 外壳与更新
│   ├── 登录、首页、导航、个人资料、帮助中心
│   └── 权限路由、主题、窄屏/桌面布局
│
├── 2. 业务应用层
│   ├── 任务域
│   │   ├── 指令核查（重点重构域）
│   │   ├── 全链条任务下发、审核、发布与批次追踪
│   │   ├── 结构化研判与无法核实流程
│   │   ├── 地址确认、小区匹配、登记关联
│   │   ├── 所内移交、任务图与个人任务流
│   │   └── 任务归档和历史查询
│   ├── 在线数据汇总（统一名称；不是日报模块）
│   ├── 工作日志与人工草稿
│   ├── 辖区档案：房屋、地址、人员、机构、标签
│   ├── 走访明细导入与走访汇总
│   ├── 平安码/管家码位置汇总
│   ├── 流程工单：类型、步骤、领取、评论、附件和事件
│   ├── 场所码：二维码、公开填报和本地主动拉取
│   └── 全民防、居住证：外部只读查询及脱敏状态
│
├── 3. Backend 应用与共享能力
│   ├── FastAPI 路由与领域服务
│   ├── 平台级 Redis 读模型查询 API：统一分页、索引组合、统计口径与 fail-closed 规则
│   ├── 身份认证、会话、岗位/权限组、社区数据范围
│   ├── 本地业务事务、revision/哈希并发控制
│   ├── 导入导出、后台任务、通知、审计、运维
│   ├── 外部只读适配器：全民防、居住证
│   └── 历史腾讯接口：业务读写已下线，仅留兼容/迁移边界
│
├── 4. 数据与持久化
│   ├── MySQL 8：本地业务权威数据
│   ├── PlatformData：账号、部门、权限、社区、审计、通知等
│   ├── OnlineData：当前本地任务、来源记录、旧兼容投影和 Outbox
│   ├── OnlineDataArchive：归档在线业务
│   ├── daily_report：历史命名的快照、任务流水及工作日志数据
│   ├── DispatchData / VisitData：下发与走访业务
│   ├── RegistryData / WorkflowData：辖区档案与流程工单
│   └── Redis：平台级派生读模型底座；当前指令核查是首个接入业务域，其他业务域待盘点
│
├── 5. 异步处理与实时派生
│   ├── 当前运行路径
│   │   ├── MySQL 业务事务
│   │   ├── `_online_projection_jobs`：Python 投影 worker
│   │   ├── `_online_summary_updates`：Python 在线汇总更新 worker
│   │   └── `_domain_event_outbox`：局部业务事件源
│   └── 目标运行路径
│       └── 各业务域 MySQL Outbox → Backend relay → Kafka → Flink → Redis 读模型 → Backend API
│
└── 6. 环境与交付
    ├── Development：代码集成与隔离架构验证
    ├── Staging：发布前业务验收和负载验证
    ├── Production：正式业务环境
    ├── Docker Compose、镜像、数据库迁移和发布流程
    └── SSH/MCP 日常诊断；复用既有部署入口，避免无必要新增网关
```

八个数据库域的详细归属以 [系统架构说明](architecture.md#八个数据库分别放什么) 为准。腾讯表已正式下线；`daily_report` 是现有物理数据库名，不代表在线汇总应继续称为“日报”。

## 指令核查重构树

指令核查相关页面和入口包括手机任务首页、任务列表、任务详情、研判、地址确认及管理侧任务查询。当前写入仍以 MySQL 业务事务为准；新架构目标是让可重建读模型与在线汇总从 Kafka/Flink/Redis 派生，MySQL 保留业务权威写入、命令记录、Outbox、目录校验和恢复用途。

```text
滨湖智慧平台
└── 任务域
    └── 指令核查
        ├── A. 命令与权威写入
        │   ├── [现行] 权威业务事实仍写入 MySQL 业务表；command record 与 Outbox 设计为同事务追加
        │   ├── [已重构] TaskCommandService.execute 是运行时任务命令唯一入口：锁定 canonical source、核对 parser/task/revision、执行授权→权威 mutation→command/outbox，并回读验证变更 revision/归档状态（task_commands.py）
        │   ├── [已重构] query.py 本地单条/批量创建、编辑、删除全部由统一执行器运行；旧腾讯创建/编辑/删除和 queue 写入返回 410
        │   ├── [已重构] mobile_tasks.py 地址人工确认/无匹配标注由统一执行器运行；普通保存、领取、分配、研判、登记和所内移交通过共用 queue_source_fields / apply_local_system_changes 命令路径
        │   ├── [已重构] fullchain_archive_jobs.py 本地归档、qmf_status_scan.py 到期归档及 qmf_source_sync.py 本地任务创建/刷新由统一执行器运行
        │   ├── [已收口] 模型三名单匹配不再改写权威“核查结果”；腾讯冲突处理、旧远端归档/回写及 enqueue_local_changes 旧队列路径明确拒绝
        │   ├── [已重构] authorize 回调必须返回策略与范围；command ledger 保存 policy、decision 和 scope SHA-256，不保存范围原值；mutation 后置读回防止空写入仍发事件
        │   ├── [已封口] 业务模块无裸 TaskCommandService.enqueue/append 入口；command/outbox 私有追加只能由 execute 调用。迁移回填是唯一显式低层基线写入
        │   ├── [现行例外] local_source 镜像、projection rebuild 和数据迁移可维护可重建缓存/索引，不代表业务命令；不得借此改写任务业务事实
        │   └── [待验证] task_command_read_model.py 已扩展授权审计字段的 measure→migrate --apply→verify；真实 MySQL 迁移、运行时结构门禁及 Dev 合成命令闭环尚未验证
        │
        ├── B. 事件合同与传输
        │   ├── [源码完成/环境待验] Outbox v4 task envelope 固定 command_id、event_id、task_id/source_id、revision、operation_id、变化类别、环境/run 与时间
        │   ├── [源码完成/环境待验] 普通事件携带 AES-GCM 快照；删除事件只携带无正文 tombstone；AAD 将命令和事件身份绑定，保留 v1/v3 只读兼容解析
        │   ├── [源码完成/环境待验] OnlineData Outbox relay → Redis Stream consumer group → Kafka delivery ledger → Kafka v4；ledger 提交后才 ACK，pending 可恢复，event_id 幂等
        │   ├── [源码完成/环境待验] Dev/Staging topic、Schema Registry JSON Schema、consumer group 与 Flink source 使用独立 v4 合同；ACL、注册状态与运行状态未验证
        │   ├── [源码完成/环境待验] Flink Redis sink 校验 command/AAD/environment/run，写入 v4 namespace，并按 revision fence 幂等处理重复、乱序和删除
        │   ├── [源码完成/环境待验] Outbox 与 command ledger 有 measure→migrate --apply→verify 结构校验；真实 MySQL 迁移未执行
        │   └── [待环境验收] Dev/Staging 真实闭环、checkpoint 恢复、DLQ 处理、重启重放和密钥轮换尚未验证
        │
        ├── C. Kafka/Flink 派生
        │   ├── [源码完成/环境待验] task read-model v4 事件由双 Kafka source 消费：元数据 reducer 与 Redis sink 使用独立 consumer group，避免 topic 分区互相分摊
        │   ├── [源码完成/环境待验] Flink JobGraph 同时包含两个 JDBC 元数据 sink 与一个 Redis task read-model sink；候选门禁核验准确 source、sink、topic、schema、group 和运行身份
        │   ├── [源码完成/环境待验] reducer 按 event_id 去重、以 revision 聚合，敏感快照不写入派生 JDBC 表；归档与删除分别统计
        │   ├── [源码完成/环境待验] Redis sink 对重复、乱序、归档快照和删除 tombstone 使用原子 revision fence；checkpoint 重启恢复未在环境验证
        │   └── [环境待验] Dev/Staging Kafka ACL、真实 JobGraph、checkpoint、Redis 写入及故障恢复证据未验证
        │
        ├── D. Redis 读模型接入（平台合同与索引能力见“平台级 Redis 读模型与 API”）
        │   ├── [本域源码接入完成/环境待验] 指令核查任务快照、七类附属状态、加密/HMAC、revision fence、来源组与 canonical 索引已进入平台读模型
        │   ├── [本域源码接入完成/环境待验] 管理侧与手机端任务、研判、导出、首页计数、assignment-selection、assignment-workbench 和详情在开关开启时以 Redis 快照为任务正文来源
        │   ├── [本域源码接入完成/环境待验] 状态域事件、索引替换、删除 tombstone、ready marker 和 fail-closed 503 已有代码路径
        │   ├── [跨域平台能力，不计入 D 的源码完成度] 复杂 grid_filters、assignment-workbench 资格/社区计数及部分 facet/filter-options 的索引化和有界分页，归属“平台级 Redis 读模型与 API”I；由平台统一建设后供各业务域使用
        │   ├── [边界明确] 人员/权限目录、社区/小区目录、权威 revision 核验及审计记录仍由各自权威存储负责；它们不是任务状态读模型回退
        │   └── [环境验收未完成] 指令核查专属 Dev/Staging API 读回、权限隔离、分页正确性、故障 503、恢复与重放证据未取得；Production 不在本轮范围
        │
        ├── E. 在线数据汇总
        │   ├── [现行] 业务语义包含每日快照、逐任务日流水、个人/社区分汇总、总汇总、区间去重、概览分类明细、筛选/分页/导出及工作日志取数；物理库 daily_report 仅为历史名称
        │   ├── [现行] local_report_scheduler 全量刷新快照和分/总汇总；online_summary_updates 增量 worker 读取权威任务并维护 _daily_task_ledger 与动态日报表
        │   ├── [部分] Task Outbox changed_fields 的 online_summary 仍只是变化类别；独立汇总事实现由增量 worker 从 ledger 事务产生
        │   ├── [源码合同已落地] 现有 builders、ledger、overview、range 与 work-log 语义已提取并固定业务日、首社区/首核查人、状态迁移、历史去重和考勤分母（docs/future-plans/online-summary-read-model.md）
        │   ├── [源码已接入/默认关闭] online_summary_updates 在更新 daily task ledger 的同一事务内写脱敏事实或 rebuild_required marker；启用需显式配置 Dev/Staging run 与独立 HMAC key（backend/services/online_summary_updates.py）
        │   ├── [源码已实现/未执行] _online_summary_fact_outbox 有 measure→migrate --apply→verify 命令；表只在显式迁移时建立（backend/migrations/online_summary_fact_outbox.py）
        │   ├── [源码已接入/未环境验收] daily_report outbox 经租约 relay、现有 Redis Stream、business_bridge 和独立 Kafka delivery topic/ledger；summary JSON Schema 与 task topic 分离
        │   ├── [未环境验收] relay 依赖同环境 daily_report 数据库名/权限与显式开关；真实 MySQL、Redis、Kafka 和 Registry 尚未验证
        │   ├── [部分] 增量 ledger 更新可产生事实；carryover/跨日快照基线和完整历史重建仍待实现
        │   ├── [待做] Flink 按事件 revision 幂等维护按业务日、业务类型、社区和人员聚合的 Redis 视图；支持重建、水位、迟到/修正事件和删除/归档
        │   ├── [待做] /stats/report、/report_range、/overview、overview details、today/reports、筛选及导出逐项切换 Redis 视图；权限过滤仍由 Backend 执行，目录名称从权威目录解析
        │   ├── [待做] 工作日志等消费者改读在线汇总 API/视图，保持既有指标口径及明确的数据水位
        │   └── [待做] 新读链路和汇总事实替代完整后，纳入平台级后台任务重构域 M 统一切换旧快照/汇总处理
        │
        └── F. 指令核查迁移与端到端验收
            ├── [部分] Outbox 字段/索引及 command ledger 已有 measure→migrate→verify 源码命令（backend/migrations/task_command_read_model.py；真实 MySQL 未验证）
            ├── [部分] Redis generation 精确成员/来源/revision 对账与 ready marker；结构版本迁移尚未闭合
            ├── [部分] task_read_model_backfill.py 可幂等发出当前有效任务的加密 Outbox 快照，未在 Dev/Staging 执行验证
            ├── [待做] 增加固定的合成业务闭环验收：现有 Dev accept 是双轨规模对账，Staging verify 是压测报告校验；二者都不是指令核查 CRUD→Outbox→Kafka→Flink→Redis→API 读回验收
            ├── [待做] 整体验收覆盖权限范围、重复/乱序事件、修改/删除 tombstone、断点恢复和敏感正文不落日志；一次完整场景报告绑定同一环境、run ID 和源码版本
            └── [待做] 先 Dev 后 Staging 执行同一固定场景，验收失败按证据修复；Production 不在本轮范围
```

## 全平台功能与设计展开树

主树列出平台现有功能、设计和长期演进项目。`[现行]`表示能力存在，不代表已经迁入 Kafka/Flink/Redis。平台级 Redis 读模型是跨业务共享的查询能力，不表示所有业务数据都应复制进 Redis；各业务域仍须按自身权威数据、查询语义和更新频率决定是否接入。

```text
滨湖智慧平台
├── A. 任务与在线业务（现行；指令核查重构状态单独展开）
│   ├── 指令核查
│   │   └── 命令、事件、Flink、Redis、在线汇总和 worker 退役
│   │       └── 细项及当前状态见“指令核查重构树”
│   ├── 在线工作表与数据查询
│   │   ├── 本地在线任务工作表：按业务类型查询、筛选、排序、分页和详情
│   │   ├── 数据查询工作台：跨业务查询、权限范围、字段裁剪和搜索
│   │   ├── 工作表协作：单元格版本、并发冲突、WebSocket 变更通知
│   │   ├── 历史腾讯工作表/来源行：正常业务读写已下线，仅保留迁移兼容与审计材料
│   │   └── 目标：共用权威任务合同及派生读模型，不允许暗中回退外部来源
│   ├── 全链条任务下发
│   │   ├── XLS/XLSX 上传、批次预览、原始数据审核
│   │   ├── 已处理数据直发校验、重复/缺项问题清单
│   │   ├── 选择任务、全选当前筛选、逐批原子校验后发布
│   │   ├── 批次详情、历史复盘、反馈导出、发布异常处置
│   │   └── DispatchData；与指令核查本地任务池的发布关系
│   ├── 在线数据汇总
│   │   ├── 单日/区间、业务类型、社区、人员、待办与完成统计
│   │   ├── 结转/新下发/已有任务变化/待完成/已完成分类
│   │   ├── 分页明细、筛选排序、完整结果导出、历史日期快照
│   │   └── 未来派生底座与指令核查共用事件/计算，但产品语义独立
│   ├── 结构化研判与无法核实
│   │   ├── 初审、深审、系统计算延时、阶段反馈
│   │   ├── 工作簿导出和逐行复用接口的结构化回导
│   │   ├── 来源版本冲突阻断、逾期推进、最终归档
│   │   └── 研判正文受保护，审计只记安全摘要
│   ├── 地址确认与小区匹配
│   │   ├── 本地规则候选、正式社区硬约束、历史小区别名
│   │   ├── 人工确认、反馈记忆、冲突复核和地址问题处理
│   │   ├── 房屋关联、来源 revision 保护、任务分配前校验
│   │   ├── 地址匹配智能化：模型只产候选，人工/规则约束不被绕过
│   │   └── 外部 Agent XLSX 标注与回导：规划路线，未开始
│   ├── 居住登记闭环
│   │   ├── 待登记自由地址和可选房屋关联
│   │   ├── 居住证外部只读查询、自动扫描和受控确认
│   │   └── 不执行外部写入，不以查询失败推断已登记
│   ├── 任务图/任务流
│   │   ├── 手机任务首页、队列、个人历史和详情
│   │   ├── 用户私有自由画布、布局和手工关系
│   │   └── 管理员 Task Flow / Rete 实验页，不改变真实业务状态
│   └── 外部任务来源与核查状态
│       ├── 全民防模型三只读来源扫描、状态刷新及来源问题统计
│       ├── 居住证模型三只读来源扫描、登记状态查询与受控确认
│       ├── 外部采集后台任务：运行状态、进度、重试与问题清单
│       └── 不越过外部只读边界，不创建外部写回、照片上传或登记流程
│
├── B. 辖区、人员与组织（现行；新架构迁移待逐项盘点）
│   ├── 辖区档案（RegistryData）
│   │   ├── 房屋/户号、地址版本、别名、小区、社区归属
│   │   ├── 房屋人员、电话、角色、机构及成员关系
│   │   ├── 来源导入批次、证件/告知书读取、冲突和问题清单
│   │   ├── 走访历史/星级关联、筛选、详情和完整结果导出
│   │   ├── 房屋身份归并、历史沿革、引用迁移、verify 与回滚设计
│   │   └── 重点人员、类别标签、分配和任务标签快照
│   ├── 人员与社区
│   │   ├── 网格人员、岗位、账号关联、多社区警务关系
│   │   ├── 社区、片区、负责人、行政区划和社区别名
│   │   ├── 部门树、用户部门关系和社区数据范围
│   │   └── 小区地址库、历史名称、停用和归并
│   ├── 出勤与周末值班
│   │   ├── 临时/长期请假历史、恢复和业务日期判断
│   │   ├── 周末值班排班、请假优先和缺排提示
│   │   └── 在线汇总及工作日志的有效人员口径
│   └── 外部出勤/请假系统：方向已确定，尚未接入
│
├── C. 走访、位置统计与工作产物（现行；新架构迁移待逐项盘点）
│   ├── 走访明细：XLSX 预览、校验、确认导入、问题清单、批次追踪
│   ├── 星级评定：只读获取、与走访明细关联、联合汇总
│   ├── 平安码/管家码：只读获取、日快照、去重、位置分类和质量核查
│   ├── 工作日志
│   │   ├── 业务日期草稿、系统取数、人员/出勤和社区数据
│   │   ├── 指令核查与走访统计、人工字段、修订冲突
│   │   └── PDF 和每日明细 XLSX 输出
│   ├── 文件能力：安全上传、预览、附件元数据、导出、下载和内容摘要
│   ├── 统计与看板
│   │   ├── 岗位仪表盘、个人/社区工作量和日期范围统计
│   │   ├── 平安码/管家码统计、走访统计和在线数据汇总
│   │   └── 后端统计口径、权限范围与导出结果保持一致
│   └── 导出与归档
│       ├── 当前筛选完整结果导出、导出记录和下载鉴权
│       ├── 全链条反馈归档、历史查询和归档文件恢复/下载
│       └── 导出任务状态、文件生命周期和敏感内容边界
│
├── D. 流程、工单与场所码（现行；新架构迁移待逐项盘点）
│   ├── 流程工单（WorkflowData）
│   │   ├── 流程类型/版本/步骤/处理人规则
│   │   ├── 创建、领取、办理、评论、提醒、附件和事件历史
│   │   └── 流程配置工作台、工单列表和详情
│   ├── 场所码
│   │   ├── 场所/二维码管理、公开登记表单、饮酒报备
│   │   ├── 一次性 token、重复提交控制、加密载荷与隐私边界
│   │   ├── VenueData 本地主动拉取、独立 Outbox、云端接收和对账
│   │   └── 云端/公网真实闭环按 venue-code-cloud-ingress 计划验收
│   └── 照片调取：业务已退役；历史工单/名单只按退役与清理合同处理
│
├── E. 外部只读集成（部分现行；所有数据仍按外部只读边界处理）
│   ├── 全民防
│   │   ├── 模型三状态扫描、单条受控状态核对
│   │   └── 不恢复登记、照片上传、反馈或外部写接口
│   ├── 居住证
│   │   ├── 首次登记/注销只读查询、有限并发扫描和错误状态
│   │   └── 账号凭据服务端加密、查询范围受配置限制
│   ├── 走访与星级来源：只读读取合同、快照和来源质量状态
│   └── 腾讯文档
│       ├── 正常业务同步、读取、写回、删行和 OAuth 已下线
│       └── 监控例外也必须为独立只读通道；历史迁移/备份只读保留
│
├── F. 平台通用用户能力（现行；新架构迁移待逐项盘点）
│   ├── 登录、会话、密码、账号安全、设备标识
│   ├── 用户、岗位、部门、权限组、账号覆盖和授权变更审计
│   ├── 岗位首页与角色仪表盘：岗位导航、待办摘要和个人/社区范围
│   ├── 通知、公告、消息中心、已读状态和受控实时提醒
│   ├── 在线状态与会话协作：presence、变更提示和断线恢复
│   ├── 个人资料、头像、公开主页、贡献日历和人员主页
│   ├── 主题、导航顺序、手机 Dock 和个性化设置
│   └── 帮助中心：内置 Markdown、在线自定义、revision 和恢复版本
│
├── G. 平台管理和运维（现行；架构迁移不是当前各模块的默认目标）
│   ├── 运维中心、审计记录/SSE、管理员任务队列
│   ├── 健康状态、脱敏诊断、备份计划/执行/校验
│   ├── 同步状态/调度、公告和系统配置
│   ├── 维护模式、受控部署、版本与客户端更新
│   ├── 环境账号、服务器 MAC、内部运行身份与入口配置
│   └── Production 只读诊断、备份/回滚/发布授权边界
│
├── H. 共享应用架构（现行实现与目标能力并存）
│   ├── FastAPI 路由、领域服务、Pydantic 请求/响应合同
│   ├── React API client、权限路由、环境身份和桌面桥
│   ├── 权限能力 + 人员/部门/社区数据范围
│   ├── MySQL 事务、连接池、revision/CAS、幂等和审计
│   ├── 导入/导出、后台 worker、定时调度、通知与文件服务
│   ├── WebSocket：在线协作版本通知，不广播业务正文
│   ├── SSE：审计/内部事件通知，断线补发后仍由 API 权限过滤
│   ├── XLS/XLSX 导入管道：解析、预览、字段映射、校验、确认、批次和问题清单
│   ├── 后台任务：队列、租约、幂等、进度、重试和失败恢复
│   ├── 实时通道：WebSocket、SSE、Redis Stream 与 Kafka 通知/派生职责边界
│   ├── 客户端壳与更新：Windows Tauri/Electron、Android 构建、安装和自更新
│   ├── 离线体验：前端离线入口、网络恢复与正式业务 API 身份校验
│   └── 安全：加密/HMAC、敏感日志过滤、附件鉴权、外部只读边界
│
├── I. 平台级 Redis 读模型与 API（跨业务共享能力；指令核查为首个接入域）
│   ├── [部分] 读模型事件、Redis 快照、主任务查询 API 和七类附属状态已由指令核查接入；完整状态见指令核查重构树 D
│   ├── [范围说明] D 只统计指令核查业务自己的读模型接入；跨业务索引原语、通用筛选/分面和有界分页由本模块负责，不回算为 D 的未完成项
│   ├── 查询合同：服务端权限范围、字段裁剪、稳定游标、canonical 去重、筛选、排序、total 与 facet 口径
│   ├── [待设计] 索引原语：按现有筛选语义选择可组合的索引能力；筛选组合不按每种组合单独建索引
│   ├── [未完成] 现有筛选项与统计项全量盘点，按业务语义分类并明确可组合索引与边界
│   ├── [未完成] 管理侧复杂 grid_filters、assignment-workbench 计数及剩余 facet/filter-options 摆脱全量扫描
│   ├── [未完成] 明确跨业务域的查询能力复用方式，并按业务域建立读模型接入与退役旧读路径清单
│   ├── 运行合同：revision fence、代际重建、ready marker、删除 tombstone、故障 503、禁止静默回退和可审计显式回滚
│   ├── 权威边界：MySQL 继续提供身份/权限、权威业务写入、source locator/revision 核验和审计；Redis 提供可重建的查询视图
│   └── [环境待验] Dev/Staging 真实分页、权限隔离、状态一致性、故障与恢复证据；不等于所有业务域已接入
│
├── J. 数据域与异步运行底座（MySQL 当前权威；Redis 派生读模型按域接入）
│   ├── PlatformData：账号、组织、权限、公告、通知、审计和出勤
│   ├── OnlineData：本地任务、来源记录、旧兼容投影、Outbox/派生输入
│   ├── OnlineDataArchive：在线业务归档
│   ├── daily_report：历史物理库名；保存快照、任务流水和工作日志材料
│   ├── DispatchData：全链条下发
│   ├── VisitData：走访导入与明细
│   ├── RegistryData：辖区房屋、地址、人员/机构关系和标签
│   ├── WorkflowData：流程定义、工单、步骤、事件、评论和附件元数据
│   ├── Redis：缓存/内部事件及平台派生读模型；当前指令核查已接入，其他业务域待盘点
│   ├── 当前异步：Python 投影 worker、在线汇总 worker、快照/导入/扫描 worker
│   └── 目标派生：MySQL Outbox → relay → Kafka → Flink → Redis → Backend API
│
├── K. 环境与长期架构设计
│   ├── 环境三元组：Production / Staging / Development 的身份、数据和版本隔离（environment-triad.md）
│   ├── Dev/Staging 投入开发：脱敏数据、业务闭环、回归、负载及晋级门槛（dev-staging-operational-readiness.md）
│   ├── 事件总线与实时计算：Outbox、Kafka、Flink、Redis、保留和回放（eventbus-realtime-infrastructure.md；dev-kafka-flink-redis-runtime.md）
│   ├── Dev Python/Flink 双轨：任务元数据投影、差异、规模和恢复观察（dev-dual-track-comparison.md）
│   ├── ORM/Repository：Repository、Unit of Work、CAS 和 Outbox；尚未实施（orm-repository-data-access.md）
│   ├── 生产接口拥堵治理：重复请求、连接池、超时、限流和后台负载（production-congestion-observability.md）
│   ├── 环境发布追溯：提交/PR/制品/数据库迁移/部署状态对照；尚未完整落地（environment-release-traceability.md）
│   ├── Agent 复杂任务追踪：阶段、阻塞、证据和持续进度；尚未实施（agent-task-progress-tracking.md）
│   ├── 地址匹配智能化：规则基线、反馈评估、候选排序；Dev 门槛前未开始（address-matching-intelligence.md）
│   ├── 外部 Agent XLSX 地址标注与回导：低复杂度探索；尚未实施（external-agent-address-annotation.md）
│   ├── 外部出勤/请假系统：接口和数据映射方向已定，尚未接入（external-attendance-leave.md）
│   ├── 场所码云端独立接收/本地主动拉取：代码存在，真实云端闭环待验收（venue-code-cloud-ingress.md）
│   ├── 腾讯旧任务模型退场：按业务域清理兼容模型，不恢复腾讯业务读写（legacy-task-model-retirement.md）
│   └── 流口标签与地址确认工作台：PR/验收台账维护具体范围和状态（flow-address-confirmation.md）

├── L. 架构合同、数据生命周期与历史边界
│   ├── 八个 MySQL 数据域及跨库稳定 ID 合同：见 database-domain-plan.md
│   ├── 在线任务本地优先写入、revision/hash、共享 Outbox 与回滚材料
│   ├── 事件保留、投递台账、重放窗口、死信和人工清理合同：见 event-retention.md
│   ├── 数据迁移合同：measure → migrate --apply → verify、备份、差异清单和回退
│   ├── 敏感字段分类、AES/HMAC、权限裁剪、日志/审计脱敏和外部只读边界
│   ├── 附件/导出物保留、删除、归档和可追溯摘要
│   ├── 已下线腾讯业务、照片调取和旧来源模型的只读兼容/清理边界
│   └── 文档与决策治理：架构说明、未来计划、执行台账、风险和部署事实分别维护

└── M. 平台级后台任务与异步执行（与指令核查并列的重构域）
    ├── [静态清单初步完成] Production 14 个常驻任务入口、按请求启动的持久作业、未接线循环及事件管道消费者已分类（docs/future-plans/legacy-worker-retirement.md）
    ├── [重点重构] 旧业务派生 worker：任务投影/任务图、在线数据汇总、快照和统计生成；随 Redis/Flink 读模型整体替换，不逐个退役
    ├── [待盘点] API 启动的长作业：归档导出、扫描、发布、来源获取等；统一任务身份、幂等、租约、进度、重试、取消和进程重启恢复
    ├── [现行/审查边界] 运维调度器：备份、性能采样、presence 清理、工作流维护等；按职责保留或治理，不为形式统一而迁入 Kafka/Flink/Redis
    ├── [现行例外/待核实] 外部只读采集与统计任务：遵守数据源下线和只读合同，不进入业务派生读模型
    ├── [待盘点] 未由主 lifespan 启动的 worker/scheduler 入口：确认调用方、部署状态和是否为死代码
    ├── [目标] 可靠任务执行底座：持久任务记录、唯一 task/run ID、租约/心跳、幂等、有限重试、DLQ/人工恢复、可观测状态
    ├── [目标] 业务派生事件底座：权威事务 + Outbox → relay → Kafka → Flink → Redis；与长作业命令执行器区分
    ├── [源码已实现/环境待验] 业务派生编排器：静态 domain registry 统一 topic/schema、Flink source/group/sink、Redis 身份和就绪合同；指令核查完整注册，在线汇总事实只注册到现有 Kafka 阶段（deploy/environments/event_pipeline/domain_pipeline.py）
    ├── [待做] 全量读写表、连接池消耗、触发频率、并发度、超时和业务所有者清单；确认服务器真实运行实例
    ├── [待做] 所有属于同一替换域的旧链路完成实现后，在 Development/Staging 整批对账、同批切换；切换后按证据修补
    └── [待做] 保留整批回滚材料与旧路径稳定期；证明新旧切换可恢复后再统一删除旧启动点、派生写入和无用队列
```

上述每一项都是架构树节点，不以单独目录或表格替代树形归属。K 为长期演进项目总表，L 为横跨业务域的数据和治理合同，M 是与指令核查并列的平台级后台任务重构域；同一设计可以同时挂在业务归属节点与合同节点。项目计划的详细范围、历史证据和状态仍以各自计划文档为准；本树维护所有功能与设计的归属、依赖和当前阶段。

## 维护规则

1. 新业务需求先落到本树的业务模块，再拆成命令、事件、读模型、权限、数据迁移和运行验收节点。
2. 状态按最小可验收节点更新；代码存在标为“已实现”，只有链路证据齐全才标为“已验收”，部署状态另记。
3. 每次标记完成都附代码入口或文档/证据链接；不能用“Dev 有 Kafka/Flink/Redis”推导所有业务都已迁移。
4. 更名业务概念时同步更新树、API/事件类别和用户文档。在线数据汇总不再称为日报；`daily_report` 仅是历史数据库名。
5. 不在树里记录凭据、真实业务正文或个人资料。
