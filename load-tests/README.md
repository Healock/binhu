# 影子压测工具

本目录只服务于已部署的影子环境，不属于正式后端运行依赖。基线固定为正式 `0.28.2` Backend 镜像 ID；不得使用“最新镜像”替代基线。

## 使用前

影子 Compose 必须使用独立项目、网络、卷、数据库密码和加密密钥。正式数据库、正式容器、互联网、腾讯、全民防、居住证、场所码云服务均不可访问。先准备环境变量：

```text
APP_ENVIRONMENT=shadow
LOAD_TEST_RUN_ID=LT-20260902-01
COMPOSE_PROJECT_NAME=binhu-loadtest-lt-20260902-01
SHADOW_DB_HOST=mysql-shadow
SHADOW_DB_NAME=LoadTest_LT_20260902_01
SHADOW_MARKER_FILE=./shadow-marker-LT-20260902-01.txt
SHADOW_BASE_URL=https://<trusted-host>/shadow-api
```

启动 Compose 后，运维人员必须从影子数据库 marker 表生成 marker 文件内容 `shadow:LT-20260902-01`，再运行命令。工具不接受 `--force` 或其他绕过目标检查的参数。

## 四阶段命令

```powershell
python .\shadowctl.py seed --run-id LT-20260902-01
python .\shadowctl.py run --run-id LT-20260902-01 --users 5 --duration 5m
python .\shadowctl.py verify --run-id LT-20260902-01
python .\shadowctl.py cleanup --run-id LT-20260902-01
```

`seed` 先生成只包含“压测”前缀虚构数据的清单；实际写入必须由影子专用 seeder/适配器执行，并在同一影子数据库中完成。这样不会凭猜测修改正式业务表结构。部署时应将影子专用 seeder 配置为只接受本清单、只连接 `SHADOW_DB_HOST`，并在 `_shadow_loadtest_marker` 写入同一 run id。`verify` 校验清单数量，并保留数据库 revision、冲突、领取和原子登记校验结果的落盘位置。

账号密码不写入清单或报告。账号密码由影子 seeder 按 `locustfile.py` 中的确定性规则生成，仅通过运行时秘密注入 Locust；生产账号和生产数据库不参与造数。

## 负载场景

Locust 账号按 50 人稳定和 75 人突发使用独立设备 ID/Cookie。列表、详情、自动保存、核查结果、领取、待登记和并发冲突均调用真实 `/shadow-api` 转发后的 `/api` 契约；409 预期冲突不会使用旧版本重试。

达到正式健康失败、OOM、可用内存低于 4GiB、负载/I/O Wait 超阈值、影子 5xx 超过 2%、自动保存 P95 超过 3 秒、连接超过 120 或发现串写/丢失/越权时，立即停止并保留最多 24 小时诊断。
