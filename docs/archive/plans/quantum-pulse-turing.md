# 滨湖智慧平台 — 大型功能扩展计划

> [!WARNING]
> 历史证据，不可直接执行。内容可能与当前源码、配置和运行环境不一致；现行规则见 [文档索引](../../README.md) 与 [风险登记](../../known-risks.md)。

## 核心决策

| 决策点 | 结论 |
|--------|------|
| 数据库架构 | 三个独立库：`OnlineData`、`OnlineDataArchive`、`daily_report`；废弃 `binhu` 库，历史数据丢弃 |
| 配置数据归属 | `OnlineData` 库内加 `_` 前缀表（`_config_spreadsheets`、`_config_oauth_tokens`、`_sync_log`） |
| 表结构 | 每种类型不同列结构，由解析器 `get_schema()` 声明，动态建表 |
| 统计规则 | 5种表格各有不同；先实现全链条，其余4种后续对接 |
| 用户管理 | 先占位 |
| 业务主键 | `身份证号 + 电话号码 + 创建时间`（格式 yyyy-MM-dd HH:mm:ss）复合键 |
| 同步模式 | 增量比对替代 DELETE+INSERT；消失的数据归档到 OnlineDataArchive |
| 日报表 | `daily_report` 库，表名 `yyyy-MM-dd_daily`，同步后自动刷新当天 |
| 前端布局 | 全局左侧边栏（4入口），设置页保留二级侧栏 |

## 数据库架构

```
MySQL 实例（共享 host/port/user/password）
├── OnlineData
│   ├── _config_spreadsheets     (表格URL配置，7种固定类型)
│   ├── _config_oauth_tokens     (腾讯文档OAuth凭据)
│   ├── _sync_log                (同步任务日志)
│   ├── t_fullchain              (全链条原始数据)
│   ├── t_rental_check           (出租房屋核查)
│   ├── t_police_stats           (涉警统计)
│   ├── t_suspect_unrevoked      (疑似未注销模型三)
│   ├── t_suspect_return         (疑似返苏)
│   ├── t_delivery_industry      (寄递业)
│   └── t_group_rental           (群租房核查)
├── OnlineDataArchive
│   ├── t_fullchain_archive      (结构与 t_fullchain 一致 + _archived_at/_archive_reason)
│   └── ... (其余6张归档表)
└── daily_report
    └── 2026-07-23_daily         (每天一张，动态创建)
```

## 多数据库连接池

`database.py` 重写为 `DatabaseManager`：
- 3个 `aiomysql.Pool`（online_data / archive / daily_report）
- `init_all()`：创建3池 + 建库建表
- `get_db(db_name)` 依赖注入：返回指定库的连接
- 跨库操作用全限定表名（`OnlineData.t_fullchain`）

`config.py` 新增：
```
MYSQL_ONLINE_DATA_DB = "OnlineData"
MYSQL_ARCHIVE_DB = "OnlineDataArchive"
MYSQL_DAILY_REPORT_DB = "daily_report"
```

## 解析器框架扩展

`BaseParser` 新增方法：
```python
@dataclass
class ColumnDef:
    name: str           # 列名（中文）
    db_type: str = "VARCHAR(500)"
    is_key: bool = False  # 是否业务主键列

def get_schema(self) -> list[ColumnDef]       # 列定义（驱动建表）
def get_business_key(self) -> list[str]        # ["身份证号","电话号码","创建时间"]
def get_stats_columns(self) -> dict            # 统计相关列（现住址/核查结果/社区/核查人）
def parse_row(self, raw_row: list) -> dict     # 在线表格行 → 标准化字典
```

表名映射：`parser_type → table_name`（如 "全链条" → "t_fullchain"）

未知类型用 `DefaultParser` 宽表兜底（20列 VARCHAR）。

## 增量比对 + 归档逻辑

```
1. 读取在线表格全部行 → parser.parse_row() → dict列表
2. 业务主键 = hash(身份证号 + 电话号码 + 创建时间)
3. 读取 OnlineData.t_xxx 当前全部行 → 构建 key→row 字典
4. 三向比对：
   - 新增：在线有、库无 → INSERT OnlineData
   - 修改：两边都有但内容不同 → UPDATE OnlineData（更新 _last_updated_at）
   - 移除：库有、在线无 → INSERT OnlineDataArchive.t_xxx_archive + DELETE OnlineData
```

## daily_report 日报表

全链条日报表列（按用户规则）：
| 列 | 字段 | 计算规则 |
|----|------|---------|
| A | 社区 | 来自全链条表 |
| B | 姓名 | 核查人姓名；汇总行固定"总计" |
| C | 数据总数 | 核查人维度 COUNT(*) |
| D | 未核查 | 现住址为空 |
| E | 已核查 | 现住址非空且核查结果为空 |
| F | 已完成 | 现住址非空且核查结果非空 |
| G | 核查完成率 | F/C |
| H | 无法见底数 | 核查结果="无法核实" + 核查结果为空 |
| I | 核查见底率 | 见底数/C（见底=已登记/无需登记/离苏/移交） |
| J | 当日人均完成数 | 同G |

行类型：核查人明细行 + 社区汇总行（B="总计"）+ 全部总计行

---

## 分阶段实现

### 第一阶段：前端边栏重构 + 数据库架构搭建

**前端（5文件）**：
- `frontend/src/components/Layout.tsx` — **重写**为左侧固定边栏（w-56），4个NavLink：在线数据汇总`/`、在线数据查询`/query`、用户管理`/users`、设置`/settings`
- `frontend/src/App.tsx` — 路由加 `/query`、`/users`；`/settings` 保留嵌套 SettingsLayout
- `frontend/src/pages/DataQuery.tsx` — **新建**占位
- `frontend/src/pages/UserManagement.tsx` — **新建**占位
- `frontend/src/pages/Dashboard.tsx` — 改名为"在线数据汇总"入口页（保留同步触发按钮，统计表格后续填充）

**后端（6文件）**：
- `backend/config.py` — 新增3个数据库名配置
- `backend/database.py` — **重写**：`DatabaseManager` 多池管理 + 3库建表DDL + `get_db(db_name)` 依赖注入
- `backend/init.sql` — **重写**：CREATE 3 DATABASE + OnlineData配置表 + 7张业务表骨架（由解析器schema驱动建表）
- `backend/main.py` — lifespan 改用 `DatabaseManager.init_all()`
- `backend/routers/spreadsheets.py` — `get_db` 改为 `get_db("online_data")`
- `backend/routers/auth.py` — 同上
- `docker-compose.yml` — MySQL env 建3个库（MYSQL_DATABASE 改为多库初始化）

### 第二阶段：数据同步框架（解析器接入 + 增量比对 + 归档）

**后端（7文件）**：
- `backend/services/parsers/base.py` — 扩展 `ColumnDef` + `get_schema`/`get_business_key`/`get_stats_columns`/`parse_row`
- `backend/services/parsers/fullchain.py` — 实现全链条的 schema + parse_row（列结构待对接确认）
- `backend/services/parsers/default.py` — 宽表兜底 schema（20列VARCHAR）
- `backend/services/parsers/__init__.py` — 增加 TABLE_NAME 映射
- `backend/services/txdocs_client.py` — `read_all_data` 参数化列数/列名，移除全局 COLUMNS
- `backend/services/sync_engine.py` — **重写核心**：增量比对 `_diff_and_upsert()` + 归档 `_archive_removed()`；移除 `_clear_raw_data`
- `backend/database.py` — 新增归档表建表DDL（7张 `_archive` 表）

### 第三阶段：统计汇总（daily_report + 全链条规则）

**后端（4文件）**：
- `backend/services/stats_calculator.py` — **重写**为 `DailyReportBuilder`：按 parser_type 分派；全链条规则实现；动态建 `yyyy-MM-dd_daily` 表
- `backend/services/parsers/fullchain.py` — 新增 `get_stats_config()`（见底判定词等）
- `backend/services/sync_engine.py` — 同步完成后调用 `DailyReportBuilder.build(today)`
- `backend/routers/stats.py` — 改为查 daily_report 库

全链条计算SQL思路：
```sql
-- 核查人明细行
INSERT INTO `2026-07-23_daily`(社区,姓名,数据总数,未核查,已核查,已完成,无法见底数,_row_type)
SELECT 社区, 核查人, COUNT(*),
  SUM(CASE WHEN 现住址 IS NULL OR 现住址='' THEN 1 ELSE 0 END),
  SUM(CASE WHEN 现住址<>'' AND (核查结果 IS NULL OR 核查结果='') THEN 1 ELSE 0 END),
  SUM(CASE WHEN 现住址<>'' AND 核查结果<>'' THEN 1 ELSE 0 END),
  SUM(CASE WHEN 核查结果 LIKE '%无法核实%' OR 核查结果 IS NULL OR 核查结果='' THEN 1 ELSE 0 END),
  'inspector_detail'
FROM OnlineData.t_fullchain GROUP BY 社区, 核查人
UNION ALL
-- 社区汇总行
SELECT 社区, '总计', COUNT(*), ... FROM OnlineData.t_fullchain GROUP BY 社区
UNION ALL
-- 全部总计行
SELECT '全部', '总计', COUNT(*), ... FROM OnlineData.t_fullchain;
```

### 第四阶段：在线数据查询 + 导出

**后端（3文件）**：
- `backend/routers/query.py` — **新建**：`/api/query/{type}?source=online|archive` 分页查询 + `/api/query/export` 导出
- `backend/services/query_service.py` — **新建**：按 type 选表选库构建查询
- `backend/requirements.txt` — 新增 `openpyxl`（Excel导出）
- `backend/main.py` — 注册 query_router

**前端（2文件）**：
- `frontend/src/pages/DataQuery.tsx` — 实现：类型选择 + 数据源切换(在线/归档) + 过滤 + 表格 + 导出按钮
- `frontend/src/api/client.ts` — 新增 queryData/exportData

### 第五阶段：用户管理占位

- `frontend/src/pages/UserManagement.tsx` — 占位"功能开发中"

---

## 关键文件路径汇总

**重写文件**：
- `backend/database.py` — 多池管理
- `backend/services/sync_engine.py` — 增量比对+归档
- `backend/services/stats_calculator.py` — daily_report生成
- `backend/services/txdocs_client.py` — 列参数化
- `backend/services/parsers/base.py` — schema框架
- `backend/init.sql` — 三库DDL
- `frontend/src/components/Layout.tsx` — 全局边栏

**新建文件**：
- `backend/routers/query.py`
- `backend/services/query_service.py`
- `frontend/src/pages/DataQuery.tsx`
- `frontend/src/pages/UserManagement.tsx`

**修改文件**：
- `backend/config.py` — 三库配置
- `backend/main.py` — lifespan + 路由注册
- `backend/routers/spreadsheets.py` — get_db改库
- `backend/routers/auth.py` — get_db改库
- `backend/services/parsers/fullchain.py` — 全链条实现
- `frontend/src/App.tsx` — 路由扩展
- `frontend/src/pages/Dashboard.tsx` — 改为汇总入口
- `docker-compose.yml` — 多库初始化

## 已确认的对接决策（2026-07-24）

1. **日报表表名格式**：每天每种类型各一张表，如 `2026-07-24_daily_fullChain`、`2026-07-24_daily_rentalHouse`、`2026-07-24_daily_suspectUnrevoked`、`2026-07-24_daily_suspectReturn`、`2026-07-24_daily_deliveryIndustry`（5种，涉警和群租房不进日报）
2. **日报表元数据表**：daily_report 库里加一张 `_daily_report_meta` 维度表，记录每个日报表的元信息：表名、报表日期、表格类型、生成方式（auto/manual）、生成时间等
3. **日报表生成时机**：定时任务自动生成 + 支持手动触发；手动生成的在元数据表里标注 `generation_method='manual'`
4. **其他6种表格列结构**：用户稍后一并提供
5. **其他4种统计规则**：和全链条稍有区别，后续一张一张对接

## 7种表格列结构（2026-07-24 用户提供）

### 1. 全链条（t_fullchain，14列）— 进日报
下发日期、截止日期、核查人、社区、来源、姓名、身份证号、电话号码、地址、创建时间、现住址、核查结果、研判、二次反馈

### 2. 出租房屋核查（t_rental_check，13列）— 进日报
下发时间、截止时间、核查人、社区、姓名、身份证号、手机号码、房屋地址、现住址、核查结果、入住方式（自购/房东出租/中介出租）、研判、二次反馈

### 3. 寄递业（t_delivery_industry，14列）— 进日报
下发时间、截止时间、核查人、姓名、身份证号、地址1、手机号码、社区、参考姓名、参考身份证号码、现住址、核查结果、研判、二次反馈

### 4. 涉警统计（t_police_stats，12列）— 仅raw入库，不处理
序号、日期、社区、简要警情及处理结果、是否开户、现住址、房屋属性、居住时间、房东信息、二房东信息、备注（是否登记）、房东是否处罚
（注：原"我所未登记"、"我所已登记"、"他所"三列已取消，不入库）

### 5. 疑似未注销模型三（t_suspect_unrevoked，9列）— 进日报
截止时间、核查人、姓名、身份证号、联系方式、地址、下发社区、核查结果、备注

### 6. 疑似返苏（t_suspect_return，12列）— 进日报
下发日期、截止日期、核查人、社区、姓名、身份证号、联系号码、高频抓拍小区、现住址、核查反馈、研判、二次反馈

### 7. 群租房核查（t_group_rental，16列）— 仅raw入库，不处理
核查人、社区、出租屋编号、出租屋地址、更新时间、居住证_居住人数(旧)、居住证_间数(旧)、居住证_床位数(旧)、核查_人数(新)、核查_房间数(新)、核查_床位数(新)、入户走访、走访日期、星级评定、责任书签订、实际情况
（注：旧数据来自居住证系统，新数据由核查人走访/电话核查后填写）

### 总汇总表
在5种分汇总表（yyyy-MM-dd_daily_xxx）基础上，每天还需一张总汇总表，统计各分汇总表的数据。格式和分汇总表基本相同，但多一列"当日人均核查数"。**后续再研究具体结构，先把分表做出来。**

### 已确认问题（2026-07-24）
1. **业务主键**：统一为"身份证号+手机号码"（各表格电话列名不同：电话号码/手机号码/联系方式/联系号码，均视为手机号码）
2. **群租房"床位数"非笔误**：旧数据（居住证系统）和新数据（核查人填写）各一组，库表列名用"居住证_"/"核查_"前缀区分
3. **总汇总表**：格式同分汇总表 + "当日人均核查数"列，后续再研究，先做分表
4. **涉警统计去掉3列**："我所未登记"、"我所已登记"、"他所"取消，不入库
> **历史归档，禁止直接执行。** 本计划描述旧版本实现，路径、端口、变量和操作顺序可能已失效。请先阅读 `docs/README.md` 与 `docs/known-risks.md`，并重新验证当前源码和运行状态。
