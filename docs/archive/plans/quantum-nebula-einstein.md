# 全链条汇总工具 -- 实现计划

> [!WARNING]
> 历史证据，不可直接执行。内容可能与当前源码、配置和运行环境不一致；现行规则见 [文档索引](../../README.md) 与 [风险登记](../../known-risks.md)。

## 架构概览

```
┌─────────────────────────┐     ┌──────────────────────────────────┐
│   前端 (React+Tailwind)  │────▶│   后端 (Python FastAPI)           │
│   Vite Dev Server       │     │                                  │
│   localhost:5173        │     │   /api/spreadsheets  CRUD        │
│                         │     │   /api/sync          同步触发     │
│   页面:                 │     │   /api/stats         统计查询     │
│   - Dashboard           │     │   /api/auth          认证管理     │
│   - Settings            │     │                                  │
└─────────────────────────┘     └───────────┬──────────────────────┘
                                            │
                              ┌─────────────┴──────────────┐
                              │                            │
                     ┌────────▼────────┐      ┌───────────▼──────────┐
                     │   MySQL 数据库   │      │  腾讯文档 OpenAPI v3  │
                     │  (aiomysql)     │      │  docs.qq.com          │
                     │                 │      │                       │
                     │  spreadsheets   │      │  GET  .../{range}     │
                     │  raw_data       │      │  POST .../batchUpdate │
                     │  daily_stats    │      │                       │
                     │  oauth_tokens   │      │  读取全链条数据 ──▶    │
                     │  sync_log       │      │  写入汇总表   ◀──     │
                     └─────────────────┘      └───────────────────────┘
```

**技术栈：**
- 后端：Python 3.11+ / FastAPI / httpx (async) / aiomysql / cryptography
- 前端：React 18 + TypeScript / Vite / Tailwind CSS / TanStack Query / react-router-dom
- 数据库：MySQL 8.0+（通过 aiomysql 异步连接池）

---

## 1. 项目目录结构

```
全链条汇总工具/
├── docker-compose.yml              # MySQL + Backend 一键启动
├── backend/
│   ├── Dockerfile                  # 后端容器镜像
│   ├── main.py                    # FastAPI 入口 + CORS + 路由挂载
│   ├── config.py                  # 环境变量读取 + MySQL 配置 (pydantic-settings)
│   ├── database.py                # MySQL 连接池 + 建表 DDL
│   ├── models/
│   │   ├── __init__.py
│   │   └── orm.py                 # dataclass + SQL 映射
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── spreadsheet.py         # Pydantic: SpreadsheetCreate/Update/Response
│   │   ├── stats.py               # Pydantic: StatsQuery/StatsResponse
│   │   └── sync.py                # Pydantic: SyncStatus
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── spreadsheets.py        # /api/spreadsheets CRUD
│   │   ├── sync.py                # /api/sync 触发 + 状态
│   │   ├── stats.py               # /api/stats 统计查询
│   │   └── auth.py                # /api/auth OAuth 管理
│   ├── services/
│   │   ├── __init__.py
│   │   ├── txdocs_client.py       # 腾讯文档 API 客户端（读+写）
│   │   ├── sync_engine.py         # 同步编排引擎
│   │   ├── stats_calculator.py    # 分组聚合统计
│   │   └── pivot_writer.py        # 写回"汇总"表
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts
│   ├── tailwind.config.js
│   ├── postcss.config.js
│   ├── tsconfig.json
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── api/
│       │   └── client.ts          # axios 实例 + 所有 API 调用
│       ├── hooks/
│       │   ├── useStats.ts
│       │   ├── useSync.ts
│       │   └── useSpreadsheets.ts
│       ├── pages/
│       │   ├── Dashboard.tsx      # 主面板
│       │   └── Settings.tsx       # 配置页
│       ├── components/
│       │   ├── Layout.tsx
│       │   ├── PivotTable.tsx     # 透视表
│       │   ├── SyncPanel.tsx      # 同步面板
│       │   ├── FilterBar.tsx      # 过滤栏
│       │   ├── SpreadsheetForm.tsx
│       │   └── OAuthForm.tsx
│       └── types/
│           └── index.ts
└── README.md
```

---

## 2. 数据库设计 (MySQL 8.0+)

> **通过 Docker Compose 一键启动 MySQL**（无需手动安装），详见下方 2.7 节。
>
> 数据库连接配置通过 `.env` 文件管理（Docker 环境下自动注入）：
> ```
> MYSQL_HOST=mysql              # docker-compose 中服务名即为 hostname
> MYSQL_PORT=3306
> MYSQL_USER=quanliantiao
> MYSQL_PASSWORD=$BINHU_MYSQL_PASSWORD
> MYSQL_DATABASE=quanliantiao
> MYSQL_POOL_SIZE=10
> ```
> 后端使用 `aiomysql` 创建异步连接池，所有查询通过连接池执行。

### 2.1 spreadsheets — 电子表格配置

```sql
CREATE TABLE IF NOT EXISTS spreadsheets (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,              -- 用户自定义名称
    file_id         VARCHAR(100) NOT NULL UNIQUE,       -- 腾讯文档 fileId
    data_sheet_id   VARCHAR(20) NOT NULL DEFAULT '000001', -- 全链条数据所在子表
    summary_sheet_id VARCHAR(50) DEFAULT '汇总',         -- 汇总结果写入的子表名
    header_row      INT DEFAULT 1,                      -- 表头行号
    enabled         TINYINT(1) DEFAULT 1,               -- 是否启用
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### 2.2 raw_data — 原始数据缓存

```sql
CREATE TABLE IF NOT EXISTS raw_data (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    spreadsheet_id  INT NOT NULL,
    row_number      INT NOT NULL,                       -- 源表行号
    下发日期         VARCHAR(50),
    截止日期         VARCHAR(50),
    核查人           VARCHAR(100),
    社区             VARCHAR(200),
    来源             VARCHAR(200),
    姓名             VARCHAR(100),
    身份证号         VARCHAR(50),
    电话号码         VARCHAR(50),
    地址             VARCHAR(500),
    创建时间         VARCHAR(50),
    现住址           VARCHAR(500),
    核查结果         VARCHAR(500),
    研判             VARCHAR(500),
    二次反馈         VARCHAR(500),
    fetched_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_spreadsheet_row (spreadsheet_id, row_number),
    FOREIGN KEY (spreadsheet_id) REFERENCES spreadsheets(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### 2.3 daily_stats — 每日统计（按表格隔离）

```sql
CREATE TABLE IF NOT EXISTS daily_stats (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    spreadsheet_id  INT NOT NULL,
    核查人           VARCHAR(100) NOT NULL,
    下发日期         VARCHAR(20) NOT NULL,              -- YYYY-MM-DD
    数据总数         INT DEFAULT 0,
    已核查           INT DEFAULT 0,
    未核查           INT DEFAULT 0,
    核查完成率       DECIMAL(6,4) DEFAULT 0.0000,
    无法核实         INT DEFAULT 0,
    移交             INT DEFAULT 0,
    已登记           INT DEFAULT 0,
    通勤             INT DEFAULT 0,
    离苏             INT DEFAULT 0,
    空白             INT DEFAULT 0,
    无法见底数       INT DEFAULT 0,                     -- = 无法核实
    核查见底率       DECIMAL(6,4) DEFAULT 0.0000,
    computed_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_stats (spreadsheet_id, 核查人, 下发日期),
    FOREIGN KEY (spreadsheet_id) REFERENCES spreadsheets(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### 2.4 oauth_tokens — 认证凭据

```sql
CREATE TABLE IF NOT EXISTS oauth_tokens (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    client_id       VARCHAR(200) NOT NULL,
    client_secret   TEXT NOT NULL,                     -- AES 加密存储
    access_token    TEXT,
    refresh_token   TEXT,
    open_id         VARCHAR(200),
    expires_at      DATETIME,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### 2.5 sync_log — 同步任务日志

```sql
CREATE TABLE IF NOT EXISTS sync_log (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    status          VARCHAR(20) DEFAULT 'pending',     -- pending|running|success|failed
    total_rows      INT DEFAULT 0,
    processed_rows  INT DEFAULT 0,
    error_message   TEXT,
    started_at      DATETIME,
    finished_at     DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### 2.6 索引

```sql
CREATE INDEX idx_raw_inspector   ON raw_data(核查人);
CREATE INDEX idx_raw_issue_date  ON raw_data(下发日期);
CREATE INDEX idx_raw_result      ON raw_data(核查结果);
CREATE INDEX idx_stats_inspector ON daily_stats(核查人);
CREATE INDEX idx_stats_date      ON daily_stats(下发日期);
```
> 注：`raw_data(spreadsheet_id)` 和 `daily_stats(spreadsheet_id)` 已有外键自动索引，无需重复创建。

### 2.7 Docker Compose 配置

`docker-compose.yml`（项目根目录）：

```yaml
version: '3.8'

services:
  mysql:
    image: mysql:8.0
    container_name: quanliantiao-mysql
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: $BINHU_MYSQL_ROOT_PASSWORD
      MYSQL_DATABASE: quanliantiao
      MYSQL_USER: quanliantiao
      MYSQL_PASSWORD: $BINHU_MYSQL_PASSWORD
    ports:
      - "3306:3306"
    volumes:
      - mysql_data:/var/lib/mysql
      - ./backend/init.sql:/docker-entrypoint-initdb.d/init.sql  # 首次启动自动建表
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost"]
      interval: 5s
      timeout: 3s
      retries: 10

  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    container_name: quanliantiao-backend
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      MYSQL_HOST: mysql          # docker-compose 内部 DNS
      MYSQL_PORT: 3306
      MYSQL_USER: quanliantiao
      MYSQL_PASSWORD: $BINHU_MYSQL_PASSWORD
      MYSQL_DATABASE: quanliantiao
      MYSQL_POOL_SIZE: 10
      ENCRYPTION_KEY: <historical-placeholder>
    depends_on:
      mysql:
        condition: service_healthy  # 等 MySQL 健康检查通过后再启动
    volumes:
      - ./backend:/app              # 开发模式：代码热更新

volumes:
  mysql_data:                       # 数据持久化
```

`backend/Dockerfile`：

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

# 暴露端口
EXPOSE 8000

# 启动
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

`backend/init.sql`（首次启动自动建表，将 2.1~2.6 的 DDL 放入此文件）：

```sql
-- 所有建表语句写在这里，容器首次启动时自动执行
CREATE TABLE IF NOT EXISTS spreadsheets ( ... );
CREATE TABLE IF NOT EXISTS raw_data ( ... );
CREATE TABLE IF NOT EXISTS daily_stats ( ... );
CREATE TABLE IF NOT EXISTS oauth_tokens ( ... );
CREATE TABLE IF NOT EXISTS sync_log ( ... );
-- 索引 ...
```

**启动方式：**
```bash
# 首次启动（会自动拉取 MySQL 镜像、建表）
docker-compose up -d

# 查看日志
docker-compose logs -f backend

# 停止
docker-compose down

# 完全清除（含数据卷）
docker-compose down -v
```

**关键设计决策：**
- `depends_on` + `condition: service_healthy`：确保 MySQL 完全就绪后后端才启动，避免连接失败
- `init.sql` 挂载到 `/docker-entrypoint-initdb.d/`：MySQL 容器首次启动时自动执行建表
- `mysql_data` 卷：容器删除后数据不丢失
- 后端 `--reload` 模式 + 代码挂载：开发时修改代码自动重启

### 2.8 database.py 连接池设计

```python
import aiomysql
from config import settings

async def init_db():
    """应用启动时创建连接池并建表"""
    pool = await aiomysql.create_pool(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_DATABASE,
        minsize=2,
        maxsize=settings.MYSQL_POOL_SIZE,
        charset='utf8mb4',
        autocommit=True,
    )
    # 建表（使用上面的 DDL）
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for ddl in DDL_STATEMENTS:
                await cur.execute(ddl)
    return pool

async def get_db():
    """依赖注入：从连接池获取连接"""
    async with db_pool.acquire() as conn:
        yield conn
```

### 2.9 SQL 语法差异（相比原 SQLite 方案的关键变更）

| 项目 | MySQL | SQLite（旧方案） |
|------|-------|------------------|
| 自增主键 | `INT AUTO_INCREMENT` | `INTEGER PRIMARY KEY AUTOINCREMENT` |
| 字符串类型 | `VARCHAR(n)` | `TEXT` |
| 浮点数 | `DECIMAL(6,4)` | `REAL` |
| 布尔值 | `TINYINT(1)` | `INTEGER` |
| 时间戳默认值 | `DEFAULT CURRENT_TIMESTAMP` | `DEFAULT (datetime('now','localtime'))` |
| 自动更新时间 | `ON UPDATE CURRENT_TIMESTAMP` | 需手动更新 |
| 引擎/字符集 | `ENGINE=InnoDB CHARSET=utf8mb4` | 无 |
| 聚合 SQL | `REAL` 改为 `DECIMAL(10,4)` | `CAST(... AS REAL)` |
| 参数占位符 | `%s` | `?` |

---

## 3. 后端 API 设计

### 3.1 电子表格管理

| Method | Path | 功能 |
|--------|------|------|
| GET | `/api/spreadsheets` | 列出所有配置的表格 |
| POST | `/api/spreadsheets` | 添加新表格 |
| GET | `/api/spreadsheets/{id}` | 查看单个表格 |
| PUT | `/api/spreadsheets/{id}` | 更新表格配置 |
| DELETE | `/api/spreadsheets/{id}` | 删除表格（级联删除关联数据） |

### 3.2 数据同步

| Method | Path | 功能 |
|--------|------|------|
| POST | `/api/sync/trigger` | 触发全量同步（后台异步执行） |
| GET | `/api/sync/status` | 获取最近一次同步状态 |
| GET | `/api/sync/history` | 同步历史记录（分页） |

### 3.3 统计查询

| Method | Path | 功能 |
|--------|------|------|
| GET | `/api/stats` | 查询统计数据（支持过滤分页） |
| GET | `/api/stats/inspectors` | 获取所有核查人列表 |
| GET | `/api/stats/date-range` | 获取数据日期范围 |

查询参数：`?spreadsheet_id=1&inspector=张三&start_date=2026-07-01&end_date=2026-07-31&page=1&page_size=50`

### 3.4 认证管理

| Method | Path | 功能 |
|--------|------|------|
| GET | `/api/auth/status` | 查看凭据状态（不含密钥） |
| POST | `/api/auth/oauth` | 保存 OAuth 凭据 |
| POST | `/api/auth/oauth/test` | 测试凭据有效性 |

---

## 4. 核心服务设计

### 4.1 TxDocsClient — 腾讯文档 API 客户端

```python
class TxDocsClient:
    BASE = "https://docs.qq.com/openapi/spreadsheet/v3"

    async def read_range(file_id, sheet_id, range_str) -> list[list]
        # GET /files/{file_id}/{sheet_id}/{range}

    async def read_all_data(file_id, sheet_id, header_row=1) -> list[dict]
        # 分页读取：每次 714 行（14列 × 714 ≈ 10000 单元格限制）
        # 返回 [{列名: 值, ...}, ...]

    async def batch_update(file_id, requests: list) -> dict
        # POST /files/{file_id}/batchUpdate
        # 每次最多 5 个操作

    async def ensure_sheet(file_id, sheet_name) -> str
        # 确保子表存在，不存在则创建，返回 sheetId
```

### 4.2 SyncEngine — 同步引擎

```
流程：
1. 创建 sync_log (status=pending)
2. 更新为 running
3. 遍历 enabled 的 spreadsheets：
   a. DELETE raw_data WHERE spreadsheet_id = ?
   b. TxDocsClient.read_all_data() 分页读取
   c. 批量 INSERT INTO raw_data
   d. 更新 processed_rows
4. StatsCalculator.recompute(spreadsheet_id) — 重新计算统计
5. PivotWriter.write(spreadsheet_id) — 写回在线文档
6. 更新 sync_log (status=success)
```

### 4.3 StatsCalculator — 统计计算

核心聚合 SQL：
```sql
INSERT INTO daily_stats (spreadsheet_id, 核查人, 下发日期, 数据总数, 已核查, 未核查,
    核查完成率, 无法核实, 移交, 已登记, 通勤, 离苏, 空白, 无法见底数, 核查见底率)
SELECT
    spreadsheet_id,
    核查人,
    下发日期,
    COUNT(*) AS 数据总数,
    SUM(CASE WHEN 核查结果 IS NOT NULL AND 核查结果 != '' THEN 1 ELSE 0 END) AS 已核查,
    SUM(CASE WHEN 核查结果 IS NULL OR 核查结果 = '' THEN 1 ELSE 0 END) AS 未核查,
    ROUND(SUM(CASE WHEN 核查结果 IS NOT NULL AND 核查结果 != '' THEN 1 ELSE 0 END) / COUNT(*), 4) AS 核查完成率,
    SUM(CASE WHEN 核查结果 LIKE '%无法核实%' THEN 1 ELSE 0 END) AS 无法核实,
    SUM(CASE WHEN 核查结果 LIKE '%移交%' AND 核查结果 NOT LIKE '%无法核实%' THEN 1 ELSE 0 END) AS 移交,
    SUM(CASE WHEN 核查结果 LIKE '%已登记%' THEN 1 ELSE 0 END) AS 已登记,
    SUM(CASE WHEN 核查结果 LIKE '%通勤%' THEN 1 ELSE 0 END) AS 通勤,
    SUM(CASE WHEN 核查结果 LIKE '%离苏%' THEN 1 ELSE 0 END) AS 离苏,
    SUM(CASE WHEN 核查结果 IS NULL OR 核查结果 = '' THEN 1 ELSE 0 END) AS 空白,
    SUM(CASE WHEN 核查结果 LIKE '%无法核实%' THEN 1 ELSE 0 END) AS 无法见底数,
    ROUND((COUNT(*) - SUM(CASE WHEN 核查结果 LIKE '%无法核实%' THEN 1 ELSE 0 END)) / COUNT(*), 4) AS 核查见底率
FROM raw_data
WHERE spreadsheet_id = %s
GROUP BY 核查人, 下发日期;
```
> 注意：MySQL 中整数除法自动返回 `DECIMAL`，无需 `CAST`，直接用 `ROUND(x, 4)` 保留 4 位小数。参数占位符使用 `%s` 而非 SQLite 的 `?`。

### 4.4 PivotWriter — 写回汇总表

```
写入"汇总"子表的列（15列）：
核查人 | 下发日期 | 数据总数 | 已核查 | 未核查 | 核查完成率 |
无法核实 | 移交 | 已登记 | 通勤 | 离苏 | 空白 |
无法见底数 | 核查见底率 | 更新时间

写入策略：
- 确保"汇总"子表存在（不存在则创建）
- 先写表头行
- 数据分批写入：每批 5 个 updateRange × 666 行（15列 × 666 ≈ 9990 单元格）
- 最后写入更新时间戳
```

---

## 5. 前端设计

### 5.1 路由
```
/          → Dashboard（主面板）
/settings  → Settings（配置页）
```

### 5.2 Dashboard 页面布局

```
┌──────────────────────────────────────────────────┐
│  全链条汇总工具                    [控制面板] [设置] │
├──────────────────────────────────────────────────┤
│  ┌─ SyncPanel ─────────────────────────────────┐ │
│  │  数据更新时间: 2026-07-22 15:35              │ │
│  │  [🔄 同步数据]  ████████░░ 80%  处理中...    │ │
│  └─────────────────────────────────────────────┘ │
│  ┌─ FilterBar ─────────────────────────────────┐ │
│  │  表格: [▼ 选择表格]  日期: [开始]~[结束]       │ │
│  │  核查人: [▼ 全部]           [查询] [重置]     │ │
│  └─────────────────────────────────────────────┘ │
│  ┌─ PivotTable ────────────────────────────────┐ │
│  │  核查人 │日期│总数│已核查│未核查│完成率│...  │ │
│  │  ───────┼────┼────┼──────┼──────┼──────┼─── │ │
│  │  张三   │7/20│ 45 │  30  │  15  │66.7% │...  │ │
│  │  张三   │7/21│ 52 │  40  │  12  │76.9% │...  │ │
│  │  ...                                      │ │
│  │                     ◀ 1 2 3 ... 10 ▶      │ │
│  └─────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────┘
```

### 5.3 关键交互
- **同步按钮**：点击触发同步 → 轮询状态（每2秒）→ 完成时刷新表格
- **过滤器**：选择表格/日期/核查人 → 自动请求 API → 表格更新
- **颜色编码**：完成率/见底率 <50% 红色，50-80% 黄色，>80% 绿色（中国股市风格）
- **空状态**：无数据时显示引导提示"请先同步数据"

---

## 6. OAuth 管理

用户在前端 Settings 页面输入：
- Client-Id（明文）
- Client-Secret（密码框）
- Access-Token（密码框）
- Refresh-Token（密码框，可选）
- Open-Id（明文）

后端：
- client_secret 使用 AES 加密存储
- 同步前检查 token 有效性
- 401 错误时标记同步失败并提示用户更新凭据

---

## 7. 错误处理

| 场景 | 策略 |
|------|------|
| API 限频 (429) | 请求间隔 200ms + 指数退避重试（最多3次） |
| Token 过期 | 同步前检查 → 过期则拒绝同步并提示 |
| 大数据量分页 | 每页 714 行（10000 单元格 / 14 列） |
| 批量写入限制 | 每 batchUpdate 5 个操作 × 666 行/操作 |
| 网络错误 | 前端 axios 30s 超时 + Toast 提示 |
| 空数据 | 前端显示空状态占位 |

---

## 8. 实现阶段

### 阶段 1：项目初始化
- 创建目录结构、依赖文件
- 编写 `docker-compose.yml`、`Dockerfile`、`init.sql`
- 实现 MySQL 连接池 + 建表 DDL（`database.py`）
- FastAPI 骨架（CORS、路由注册、启动时初始化连接池）
- 验证：`docker-compose up -d` 一键启动

### 阶段 2：后端核心服务
- TxDocsClient（读取 + 写入）
- SyncEngine
- StatsCalculator
- PivotWriter

### 阶段 3：后端 API 路由
- spreadsheets CRUD
- sync 触发/状态
- stats 查询
- auth 管理

### 阶段 4：前端开发
- Vite + React + Tailwind 脚手架
- Dashboard 页面（SyncPanel + FilterBar + PivotTable）
- Settings 页面（SpreadsheetForm + OAuthForm）

### 阶段 5：联调测试
- 端到端流程验证
- 错误场景覆盖
- UI 完善
> **历史归档，禁止直接执行。** 本计划描述旧版本实现，路径、端口、变量和操作顺序可能已失效。请先阅读 `docs/README.md` 与 `docs/known-risks.md`，并重新验证当前源码和运行状态。
