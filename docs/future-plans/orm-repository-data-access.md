# ORM 与 Repository 数据访问分层

- 文档类型：数据访问架构长期演进计划
- 当前状态：已确定方向，等待 Kafka/Flink/Redis 运行路径盘点；尚未引入 ORM，也未迁移现有业务 SQL
- 计划边界：本文件只定义后续重构范围、约束、验收和拆分方式，不代表代码、数据库、事件流或生产环境已经改变
- 关联计划：[事件总线与实时计算基础设施](eventbus-realtime-infrastructure.md)、[Dev Kafka、Flink 与 Redis 架构升级](dev-kafka-flink-redis-runtime.md)

## 目标

在 MySQL 继续作为业务数据唯一权威来源的前提下，把业务代码从直接编写 SQL 逐步迁移到 Model、Repository、Query Object 和 Unit of Work。普通 CRUD 可以使用 ORM，涉及并发、批量、迁移和复杂统计的场景继续使用集中管理的受控 SQL。

长期目标是：

```text
Router / Worker / Scheduler
        ↓
Service / Use Case
        ↓
Repository
        ├─ 普通 CRUD：ORM
        ├─ 并发和批量：受控 SQL
        └─ 复杂统计：Query Repository 或 Flink SQL
        ↓
MySQL 业务事务
        ↓
Outbox → Kafka → Flink → Redis / 派生结果
```

这项计划的重点不是让仓库中完全没有 SQL，而是让 SQL 集中到可以审查、测试、观测和优化的边界内。Router、Service、Worker 和 Scheduler 不应继续散落表名、列名、占位符和事务细节。

## 与 Kafka、Flink 和 Redis 架构的关系

事件流架构改变了 SQL 优化的优先级，但没有取消 Repository 的必要性。

### 由事件流逐步承接的职责

任务投影、可重建汇总、事件转发、实时计数和其他派生计算可以逐步移到 Kafka/Flink/Redis。这样可以减少后台派生任务与用户保存之间的 MySQL 锁竞争，避免把可重建计算和核心业务事务放在同一个数据库事务中。

但“计划由 Flink 承接”不能等同于“当前生产已经完全切换”。在每个派生域确认以下条件之前，旧 MySQL worker、投影队列和兼容路径必须保留并明确标记：

- 新路径已经覆盖旧路径的业务输入和输出；
- 重复事件、乱序 revision、重放、checkpoint 恢复和消费者重启已经验证；
- 新旧结果有独立命名空间和双轨比对；
- 没有旧路径与新路径同时写同一个派生结果；
- 事件中不包含姓名、证件号、手机号、完整地址、备注正文、照片、令牌或外部平台正文；
- 事件流异常时，查询可以回到 MySQL 权威数据，或明确返回派生结果暂不可用；
- Dev、预发布和生产的事件流、数据库、Redis、topic、checkpoint 和凭据保持环境隔离。

当前已存在的事件流计划仍以对应文档和实际验收记录为准。新架构尚未授权直接关闭生产 Python worker，也不授权把 Kafka、Flink 或 Redis 变成业务主数据源。

### 仍然必须由 MySQL 负责的职责

- 用户之间对同一业务对象的并发控制；
- revision、row hash 和乐观锁检查；
- 必要的 `FOR UPDATE` 业务锁；
- 用户保存、领取、分配、归档和登记等业务事务；
- 权限、社区范围和状态机约束；
- 业务主表和必要的事务投影；
- 与业务写入同事务的 outbox 记录；
- 需要精确原子性的幂等和冲突处理。

因此，新架构减少的是“后台派生任务与用户保存抢锁”，不会消除“用户 A 与用户 B 同时修改同一条业务记录”的并发问题。

## 数据访问分层

### Router

Router 只负责：

- 解析请求和 Filter Model；
- 调用权限、社区范围和账号状态检查；
- 调用 Service 或 Use Case；
- 将结果转换为 API 响应。

Router 不应新增：

- 直接 `cursor.execute()`；
- 表名、列名和 schema 名；
- 手工拼接 `WHERE`、`ORDER BY` 或 `LIMIT`；
- 隐式 `FOR UPDATE`；
- 业务事务提交和回滚细节。

### Service / Use Case

Service 负责业务编排，例如：

- 校验状态转换；
- 检查权限和社区范围；
- 调用多个 Repository；
- 决定是否需要锁或 revision；
- 组织 outbox 事件；
- 处理跨域补偿和失败状态。

Service 不应直接构造 SQL。跨数据库流程不能因为由一个 Service 调用多个 Repository 就被描述为跨库原子事务；需要通过 outbox、补偿和幂等状态表达真实边界。

### Repository

Repository 是数据访问边界。它负责：

- ORM Model 或受控 SQL 的选择；
- 参数绑定和动态标识符白名单；
- 数据库连接和事务上下文的使用；
- 行到 Model 的转换；
- 查询分页和排序契约；
- 锁、revision、幂等和影响行数检查；
- 不含业务正文的查询耗时和错误指标。

建议按业务域组织目录：

```text
backend/
  repositories/
    platform/
    registry/
    online/
    workflow/
    common/
  models/
  queries/
    reports/
    legacy/
```

`queries/legacy/` 只用于暂存已经确认属于历史兼容路径、尚未下线的查询。它不是新业务继续依赖旧投影队列的理由。

## ORM 的适用范围

### 适合先使用 ORM 的场景

- 单表或简单关联的详情读取；
- 普通分页列表；
- 社区、网格、人员和机构字典；
- 饮酒报备和场所登记的查询详情；
- 普通通知、帮助文档和审计摘要；
- 不涉及复杂锁和跨库事务的简单业务修改。

ORM Repository 必须显式控制：

- loader strategy，避免 N+1 查询；
- session 生命周期，不跨请求、worker 或 Kafka 事件共享；
- autoflush 行为；
- 只读取需要的列，避免意外加载敏感正文；
- 数据库 schema 和业务域连接池；
- 时间统一按 UTC 解析，再按系统时区展示。

### 继续使用受控 SQL 的场景

- `FOR UPDATE`、`FOR UPDATE SKIP LOCKED` 和锁顺序敏感的逻辑；
- `revision` 条件更新和影响行数检查；
- 批量插入、批量更新、批量归档和导入；
- 复杂统计、窗口函数、快照和跨表聚合；
- generated column、JSON 路径和 MySQL 专用能力；
- 数据库迁移、索引和 schema 检查；
- outbox、ACK、租约、幂等和 uncertain 状态写入；
- 需要精确控制执行计划的高频查询。

这些 SQL 也必须位于 Repository、Query Object、Migration 或明确的基础设施模块中，而不是散落在 Router 和 Worker。

## 显式表达并发语义

不能用一个通用的 `get()` 或 `save()` 隐藏所有并发行为。Repository 方法名称必须体现锁和版本语义：

```python
class OnlineTaskRepository:
    async def get(self, task_id: int) -> OnlineTask | None:
        ...

    async def get_for_update(self, task_id: int) -> OnlineTask | None:
        ...

    async def update_if_revision_matches(
        self,
        task_id: int,
        changes: TaskChanges,
        *,
        expected_revision: int,
    ) -> OnlineTask:
        ...

    async def claim_pending_batch(
        self,
        *,
        actor_id: int,
        limit: int,
    ) -> list[OnlineTask]:
        ...
```

普通 `get()` 不应隐式加锁；`update_if_revision_matches()` 必须检查影响行数；批量领取必须显式声明是否使用租约、锁和跳过已锁定行。

## Unit of Work 和 Outbox

建议提供轻量 Unit of Work，统一连接获取、事务和指标：

```python
async with unit_of_work("online_data") as uow:
    task = await uow.online_tasks.get_for_update(task_id)
    updated = await uow.online_tasks.update_if_revision_matches(
        task,
        changes,
    )
    await uow.outbox.append(updated.event)
```

Unit of Work 负责：

- 有限超时获取连接；
- `BEGIN`、`COMMIT`、`ROLLBACK`；
- pool wait 和事务持续时间指标；
- 连接 session timezone 为 UTC；
- 事务结束后释放连接。

Unit of Work 不负责直接发送 Kafka。业务事务应当：

1. 更新 MySQL 业务表；
2. 在同一个数据库事务内写 outbox；
3. 提交；
4. 由独立 relay 发布 Kafka；
5. 由 Flink 计算派生结果；
6. 由 Redis 提供带 revision fence 的缓存读取。

不能在 MySQL 提交前或提交后直接把 Kafka 网络调用塞进业务事务，否则会产生“数据库已提交但事件未发送”或“事件已发送但数据库回滚”的窗口。

## 查询对象和安全 SQL 构造

所有动态查询先经过结构化 Filter Model，再由 Query Builder 或 Repository 编译为参数绑定 SQL：

```text
请求参数
  → Pydantic Filter Model
  → QuerySpec
  → SQL Compiler / ORM expression
  → 参数绑定查询
```

建议建立统一工具：

```python
@dataclass(frozen=True)
class SqlQuery:
    text: str
    params: tuple[Any, ...]


def in_clause(values: Sequence[Any]) -> tuple[str, tuple[Any, ...]]:
    ...


def quote_identifier_from_whitelist(
    value: str,
    allowed: Collection[str],
) -> str:
    ...
```

约束如下：

- 业务值永远使用参数绑定；
- 表名、列名和排序字段只能来自固定白名单；
- `IN` 只动态生成占位符，不拼接真实值；
- 新代码不增加 `SELECT *`；
- 新代码不增加未统一校验的 `NOW()`；
- 新业务代码不依赖隐式 DomainRoutingCursor 改写短表名；
- 复杂 SQL 必须有查询结果契约和测试；
- SQL fingerprint、耗时和错误指标不能记录参数或业务正文。

## 迁移顺序

### 第 0 阶段：核对事件流实际接管范围

输出一份能力矩阵，逐项记录：

| 能力 | 当前执行方 | 旧 MySQL 路径 | Kafka/Flink 路径 | 开关 | 是否可移除 |
| --- | --- | --- | --- | --- | --- |
| 任务投影 | 待核对 | 待核对 | 待核对 | 待核对 | 待定 |
| 汇总统计 | 待核对 | 待核对 | 待核对 | 待核对 | 待定 |
| 事件转发 | 待核对 | 待核对 | 待核对 | 待核对 | 待定 |
| 用户保存 | MySQL 业务事务 | 必须保留 | 产生事件 | 必须开启 | 不移除 |
| 归档 | MySQL 业务事务 | 必须保留 | 产生事件 | 必须开启 | 不移除 |

本阶段只做代码、配置和验收证据盘点，不关闭生产 worker，不删除旧表，不修改真实业务数据。

### 第 1 阶段：建立数据访问基础设施

- 增加 `SqlQuery`、标识符白名单和占位符工具；
- 增加 Repository 协议和返回类型约束；
- 增加统一连接获取超时；
- 增加 pool wait、事务时长和安全错误指标；
- 新连接固定 session timezone 为 UTC；
- 增加禁止 Router/Worker/Scheduler 新增直接 SQL 的静态检查；
- 不改变现有业务接口和数据库结构。

### 第 2 阶段：只读 Repository 试点

优先从边界清晰的模块开始：

1. 饮酒报备查询；
2. 场所登记记录查询；
3. 管理员审计列表；
4. Workflow 工单列表。

试点必须同时提供 ORM 方案与现有受控 SQL 的结果对照，确认：

- API 响应完全一致；
- 权限、社区范围和字段脱敏一致；
- 查询次数没有增加；
- 没有 N+1；
- 事务时长不增加；
- 分页、排序和导出使用相同查询契约；
- 真实敏感字段没有被无必要地加载或记录。

### 第 3 阶段：普通 CRUD

试点通过后，再迁移：

- 用户、社区、网格和人员字典；
- Registry 房屋、人员和机构普通查询；
- Workflow 工单基础详情；
- QR/饮酒报备配置和查询；
- 普通通知和帮助文档。

每个 Repository 需要配套 Model、Filter、分页、权限范围、时间处理和错误契约。

### 第 4 阶段：高频查询和派生读取

再处理：

- 在线任务查询；
- 手机任务列表；
- 地址匹配候选；
- 汇总和统计读取；
- Redis 派生结果读取。

这一步必须先有 fingerprint、耗时、rows examined、锁等待和缓存命中基线，再改投影列、generated column、索引或 cursor 分页。

### 第 5 阶段：核心写路径

最后迁移：

- 在线任务保存；
- 领取和分配；
- 场所登记和饮酒报备落库；
- 归档；
- outbox 写入；
- 云端拉取、ACK、幂等和 uncertain 处理。

本阶段的验收重点是版本冲突、锁顺序、事务范围、幂等和失败恢复，而不是 ORM 代码比例。任何 ORM 生成的 SQL 都必须通过真实数据库驱动和测试数据库验证。

## ORM 技术选择

若进入 ORM 试点，优先评估 SQLAlchemy 2.x Async，并允许 ORM 与 SQLAlchemy Core 混用。选择前必须验证：

- 多业务数据库和 schema 路由；
- 中文列名和历史兼容列；
- `with_for_update()` 语义；
- revision 条件更新；
- 连接池和现有 aiomysql 的边界；
- async session 生命周期；
- lazy loading 和 N+1；
- autoflush；
- 事务回滚和异常分类。

不先把整个项目改为 ORM，不在 ORM 试点中同时改数据库结构、Kafka 合同、Flink 作业、Redis key 或权限模型。

## 验收和观测

每个 Repository 试点至少要有：

- Model 到数据库行的映射测试；
- 空结果、分页边界和排序稳定性测试；
- 权限和社区范围测试；
- 版本冲突和重复提交测试（写路径）；
- SQL 参数化和动态标识符安全测试；
- 查询次数/N+1 断言；
- 事务提交、回滚和连接释放测试；
- 真实 MySQL 测试数据库验证。

生产只读观测应记录：

- route 或 repository 名称；
- db domain；
- SQL fingerprint；
- duration；
- pool wait；
- transaction duration；
- returned/affected rows；
- deadlock、lock wait timeout 和安全错误分类。

不得记录 SQL 参数、姓名、证件号、手机号、地址、照片、令牌、Cookie 或业务正文。

新架构还需要同时观察：

- Kafka lag；
- Flink checkpoint 和恢复状态；
- Redis hit/miss；
- revision fence 拒绝数；
- outbox pending、retry、dead letter；
- MySQL 业务事务延迟和锁等待。

不能用“Kafka/Flink 正常运行”替代 MySQL 业务事务验收，也不能用“ORM 查询成功”替代事件流一致性验收。

## 回滚和停止条件

发生以下情况时停止扩大 Repository 或 ORM 迁移范围：

- API 返回字段、权限范围或排序发生变化；
- ORM 生成额外 N+1 查询；
- 事务时长、锁等待或连接池等待增加；
- revision 冲突被覆盖；
- MySQL 业务写入依赖 Kafka、Flink 或 Redis 成功；
- 同一业务事件被旧路径和新路径重复处理；
- Redis 结果覆盖更高 revision；
- 事件或日志泄露敏感正文；
- 真实 MySQL 驱动行为与模拟测试不一致；
- 无法区分业务主表和派生结果来源。

回滚优先恢复原 Repository/SQL 路径和原查询开关，不自动恢复数据库备份，不删除 outbox、Kafka、Flink、Redis 或失败证据。数据库结构迁移和业务代码发布必须拆分，不能依赖代码回滚自动逆向 schema。

## 明确不做

- 不把 Kafka、Flink 或 Redis 设为业务主数据源；
- 不因为 ORM 试点关闭现有生产派生 worker；
- 不一次性重写整个项目；
- 不隐藏 `FOR UPDATE`、revision、幂等和跨库补偿语义；
- 不把复杂报表和批量操作强行 ORM 化；
- 不删除腾讯历史兼容材料或恢复腾讯正常业务读取；
- 不在没有真实 MySQL 验证时声称生产性能已改善；
- 不在计划 PR 中修改数据库、事件流、Redis key、权限或部署配置。

## 完成标准

该长期计划不能仅以“ORM 已安装”作为完成条件。至少应满足：

- 新增业务代码不在 Router、Worker、Scheduler 中直接写 SQL；
- 普通 CRUD 已有稳定 Model 和 Repository；
- 锁、revision、批量、outbox 和复杂统计有明确受控实现；
- Repository 查询有权限、分页、事务、N+1 和真实 MySQL 验证；
- Kafka/Flink/Redis 派生边界有实际事件流证据，不依赖口头架构假设；
- MySQL 仍然是业务事务唯一权威来源；
- 失败、回滚、重放和环境隔离均有记录；
- 计划中的未完成项目仍明确标记为未完成。

本文件的后续代码实现应拆成多个独立 PR，至少分为数据访问基础设施、只读 Repository 试点、普通 CRUD、查询性能、核心写路径和旧派生路径清理，不能合并成一个无法回退的大型重构。
