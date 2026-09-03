# 影子压测工具

本目录只服务于隔离的影子环境，不属于正式后端依赖。它会创建虚构账号、社区、小区和 3,600 条虚构流口任务，并通过正式客户端使用的 `/shadow-api` 契约执行压力测试。

## 安全边界

- 只允许 `APP_ENVIRONMENT=shadow`。
- Compose 项目名必须和运行编号严格对应。
- 操作端只允许连接宿主机 `127.0.0.1:47126` 的影子 MySQL。
- 数据库名必须以 `LoadTest_` 开头。
- Backend、MySQL、Redis 镜像必须固定为精确 `sha256` ID 或 digest，禁止 `latest`。
- 影子 Docker 网络为 `internal`，腾讯、全民防、居住证、场所码云服务全部关闭。
- 工具没有 `--force`；任何安全检查失败都会立即停止。
- 工具不会连接正式数据库。正式库“零压测数据”必须由运维另行做只读扫描，并把结果作为证明文件交给 `verify`。

PR #489 的 `/shadow-api` 路由和客户端影子入口是执行前置条件。Nginx 只把同域 `/shadow-api/` 转发到宿主机 `127.0.0.1:47125`，不得暴露 MySQL 的 47126 端口。

## 环境变量

以下示例中的密码和密钥必须使用本次运行的随机值，不得复用正式环境：

```text
APP_ENVIRONMENT=shadow
LOAD_TEST_RUN_ID=LT-20260902-01
COMPOSE_PROJECT_NAME=binhu-loadtest-lt-20260902-01

SHADOW_BACKEND_IMAGE=sha256:<正式 0.28.3 Backend 的完整镜像 ID>
SHADOW_MYSQL_IMAGE=mysql@sha256:<已核对的完整 digest>
SHADOW_REDIS_IMAGE=redis@sha256:<已核对的完整 digest>

SHADOW_DB_HOST=127.0.0.1
SHADOW_DB_PORT=47126
SHADOW_DB_NAME=LoadTest_LT_20260902_01
SHADOW_DB_USER=<影子专用用户>
SHADOW_DB_PASSWORD=<影子专用密码>
SHADOW_MYSQL_ROOT_PASSWORD=<影子专用 root 密码>
SHADOW_REDIS_PASSWORD=<影子专用 Redis 密码>
SHADOW_ENCRYPTION_KEY=<影子专用加密密钥>
SHADOW_BOOTSTRAP_PASSWORD=<仅用于首次启动的临时密码>
SHADOW_OPS_AGENT_TOKEN=<影子专用运维采集令牌>

SHADOW_BASE_URL=https://<现有可信域名>
PRODUCTION_HEALTH_URL=https://<现有可信域名>/api/health
PRODUCTION_CONTAINER_NAMES=binhu-backend,binhu-mysql,binhu-redis
```

`SHADOW_BASE_URL` 只填写 HTTPS origin，不带 `/shadow-api`。负载脚本固定追加 `/shadow-api`，因此无法被配置成任意外部路径。

影子环境包含独立的只读运维采集器，只允许读取当前运行编号下的 Backend、MySQL 和 Redis 容器状态。采集器与正式运维网络隔离，令牌必须使用本次运行的随机值；运维中心出现“容器状态暂时不可用”时不得开始正式压测，因为此时无法完整判断 OOM、重启和资源饱和。

## 启动和造数

先安装工具依赖，再启动独立 Compose：

```powershell
python -m pip install -r .\requirements.txt
docker compose -p $env:COMPOSE_PROJECT_NAME -f .\docker-compose.shadow.yml up -d
python .\shadowctl.py seed --run-id $env:LOAD_TEST_RUN_ID
python .\shadowctl.py verify --run-id $env:LOAD_TEST_RUN_ID
```

`seed` 会执行三层检查：

1. 宿主机目标必须是固定的影子 MySQL 端口；
2. 数据库必须带有 Compose 初始化的影子标记；
3. 造数脚本必须在精确 Backend 镜像内部运行，并再次核对 `APP_ENVIRONMENT`、运行编号和数据库主机。

随后脚本通过正式 `create_local_source_row()`、待登记房屋关联和投影服务写入数据。任务使用生产正常识别的 `local_table` 来源类型，并通过唯一的 `shadow:<run_id>:task:<ordinal>` 来源引用限制在当前影子运行内；正式业务代码不增加影子专用来源分支。任务按每批 100 条提交，重复执行不会重复创建未修改的数据；如果该运行已被压测修改，重新 seed 会因内容冲突而停止，不能偷偷重置结果。

造数完成后会生成：

- `shadow-fixture-<run_id>.json`：不含密码的虚构数据清单；
- `shadow-runtime-<run_id>.json`：由真实数据库返回的任务主键、来源 ID、房屋 ID 和版本。

观察账号密码不写入清单和报告。需要首次手工验收时，在安全终端临时查看：

```powershell
python -c "from fixture import password_hint; print(password_hint('observer@shadow'))"
```

正式客户端输入 `observer@shadow` 后必须显示橙色“影子压测环境”标识；影子入口关闭时必须停留在登录页，不得回退正式环境。

## 固定执行顺序

### 1. 5 人冒烟，5 分钟

```powershell
python .\shadowctl.py run --run-id $env:LOAD_TEST_RUN_ID --scenario mixed --users 5 --duration 5m
```

### 2. 50 人集中登录，5 分钟

```powershell
python .\shadowctl.py run --run-id $env:LOAD_TEST_RUN_ID --scenario login --users 50 --duration 5m
```

登录场景在约 5 秒内启动 50 个核心账号，登录一次后即停止该虚拟用户，不混入业务请求。

### 3. 50 人稳定业务，30 分钟

```powershell
python .\shadowctl.py run --run-id $env:LOAD_TEST_RUN_ID --scenario mixed --users 50 --duration 30m
```

50 人场景以约 5 分钟从 0 增加到 50 人。虚拟用户按计划执行列表、详情、自动保存、核查结果、领取和待登记原子保存；每人间隔 2～6 秒。

### 4. 20 人并发冲突专项，10 分钟

```powershell
python .\shadowctl.py run --run-id $env:LOAD_TEST_RUN_ID --scenario conflict --users 20 --duration 10m
```

冲突场景固定为单个 Locust 进程。20 个账号组成 10 对，在读取同一 revision 后通过进程内屏障同时提交；每轮必须恰好一个成功、一个 409，409 不使用旧版本自动重试，也不计入系统错误率。不要用 Locust master/worker 分布式模式运行此场景。

### 5. 75 人突发，5 分钟

```powershell
python .\shadowctl.py run --run-id $env:LOAD_TEST_RUN_ID --scenario mixed --users 75 --duration 5m
```

75 人场景使用 50 个核心账号和 25 个突发账号，在约 5 秒内达到目标并把操作间隔缩短到 0.5～2 秒。它验证 75 人同时在线的短时突发，不用于宣称平台最大容量。

## 自动停止与报告

每次 `run` 都同时记录主机、正式健康、正式容器、影子容器、影子 MySQL 和 Locust 指标。出现以下任一情况会终止 Locust 并写入 `*-stop-reason.json`：

- 主机可用内存低于 4GiB；
- Swap 比运行开始增加超过 256MiB；
- 一分钟负载超过 28 或 I/O Wait 超过 20% 并持续一分钟；
- 正式健康连续失败两次；
- 指定的正式容器停止、重启中或发生 OOM；
- 影子 MySQL 连接超过 120 或运行线程超过 30；
- 影子数据库表和索引总量达到 10GiB；
- 至少 100 次请求后，非预期失败率超过 2% 并持续一分钟；
- 普通自动保存 P95 超过 3 秒并持续两个采样窗口。

Docker 命名卷在不同宿主机上没有统一可靠的硬配额。本工具会以数据库表和索引大小达到 10GiB 为停止线；若生产服务器必须具备硬配额，应在执行前把 MySQL 数据目录放到预先创建的 10GiB quota-backed 文件系统。不能把这里的软停止描述成硬容量限制。

输出包括 Locust HTML/CSV、逐阶段 JSONL 指标、虚构写入事件和停止原因。事件只包含压测账号、任务定位、revision 及虚构变更，不包含正式人员资料。

## 最终一致性校验

```powershell
python .\shadowctl.py verify --run-id $env:LOAD_TEST_RUN_ID --production-proof .\artifacts\production-zero-proof.json
```

`verify` 检查：

- 76 个影子账号、3,600 条来源、3,600 条投影、48 套房屋；
- 每个成功响应的 revision 必须大于读取 revision；
- 每个字段最后一次成功写入值必须与数据库一致；
- 待登记房屋 ID、版本和关联状态一致；
- 自主领取任务最终只有一个成功领取人，且投影核查人与成功方一致；
- 每轮协调冲突恰好一个 200、一个 409。
- 冲突任务最终字段值必须来自成功方，不能落入 409 失败方的草稿。

正式库只读证明格式：

```json
{
  "run_id": "LT-20260902-01",
  "checked_at": "2026-09-02T10:00:00+08:00",
  "checked_scopes": [
    "shadow_source_refs",
    "shadow_usernames",
    "loadtest_prefixes",
    "legacy_shadow_source_kind"
  ],
  "scope_counts": {
    "shadow_source_refs": 0,
    "shadow_usernames": 0,
    "loadtest_prefixes": 0,
    "legacy_shadow_source_kind": 0
  },
  "matching_rows": 0
}
```

证明文件只能来自独立、只读的正式库检查，并且必须分别确认：不存在 `shadow:%` 来源引用、不存在 `@shadow` 后缀账号、不存在压测前缀社区/人员/业务数据，也不存在旧的 `shadow_loadtest` 来源类型。本工具故意不接受正式数据库连接参数，避免“为了证明没串写，反而让压测工具具备正式库访问能力”。

## 清理

用户退出 `observer@shadow` 后执行：

```powershell
python .\shadowctl.py cleanup --run-id $env:LOAD_TEST_RUN_ID
```

清理只会删除名称与 run ID 严格匹配的 Compose 项目、数据卷和本次 artifacts。失败环境最多保留 24 小时用于诊断，随后仍使用同一精确命令删除，禁止手工扩大通配范围。
