# 滨湖智慧平台运维与开发手册

本手册只使用变量名和占位符。真实凭据保存在各电脑自己的环境配置或密码管理器中，不得写入共享文档、命令历史或 Git。

## 本地配置

两台电脑分别维护自己的 `.env`、`backend/.env` 和可选的 `AGENTS.local.md`；不得通过 Git、聊天或共享网盘复制这些文件。

根目录 `.env.example` 是 Docker Compose 模板：

```powershell
Copy-Item .env.example .env
# 编辑 .env，将所有 replace-with-* 占位值替换为本机值
docker compose config --quiet
```

直接从 `backend/` 启动 FastAPI 时，`backend/.env` 使用后端字段名，不能原样复制 Compose 模板：

```dotenv
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=binhu
MYSQL_PASSWORD=<local-application-password>
MYSQL_ONLINE_DATA_DB=OnlineData
MYSQL_ARCHIVE_DB=OnlineDataArchive
MYSQL_DAILY_REPORT_DB=daily_report
ENCRYPTION_KEY=<local-application-key>
```

部署辅助脚本读取进程环境中的 `BINHU_SSH_*`，不会自动加载根目录 `.env`。PowerShell 示例：

```powershell
$env:BINHU_SSH_HOST = "<production-host>"
$env:BINHU_SSH_PORT = "<ssh-port>"
$env:BINHU_SSH_USER = "<ssh-user>"
$env:BINHU_SSH_PASSWORD = "<ssh-password>"
python upload.py
```

不要把上述真实值保存到脚本、共享 Markdown 或 shell profile。

## 本地开发

前端：

```powershell
Set-Location frontend
npm install
npm.cmd run dev
npm.cmd run build
```

后端需要可用的 Python 环境、依赖和三个 MySQL 数据库：

```powershell
Set-Location backend
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

Docker Compose 可用于本地一体化运行，但当前 Compose 的端口发布策略不适合作为生产安全基线，部署前必须先阅读 [风险登记](known-risks.md)。

## 家庭与单位双地点 Git 流程

开始新任务：

```powershell
git switch main
git pull --ff-only
$branchName = "feat/example-topic"
git switch -c $branchName
git push -u origin $branchName
```

离开当前电脑前：

```powershell
git status
git add -- path/to/reviewed-file
git commit -m "feat: describe the completed slice"
git push
```

另一台电脑首次继续该分支：

```powershell
git fetch origin
git switch --track origin/feat/example-topic
```

本机已有该分支时：

```powershell
git switch feat/example-topic
git pull --ff-only
```

完成后通过 GitHub PR 合并，或在确认 `main` 最新且验证通过后执行非强制合并。不要创建地点分支，不要依赖 `stash` 跨电脑交接，不要对共享分支强制推送。

## 生产部署门禁

当前生产入口、MySQL 暴露和默认凭据仍在第二阶段整改范围内。在这些风险关闭前，不得把现有 Compose/nginx 配置当作已验证的生产模板自动部署。

任何生产部署都必须按顺序满足：

1. 用户明确授权本次部署目标和范围。
2. `main` 与 `origin/main` 同步，工作树干净，目标提交可追溯。
3. 前端生产构建、Python 检查和 Compose 配置解析通过。
4. 备份三个数据库，并确认备份文件非空、时间和目标正确。
5. 保存远程配置与回滚所需版本，不覆盖远程 `.env` 或数据卷。
6. 使用受控上传或 Git 拉取更新源码，重建必要服务。
7. 验证健康接口、入口响应、容器状态、日志、登录、OAuth 状态和关键表数据。
8. 失败时优先回滚应用，不删除卷或重建数据库。

## 备份与恢复

在服务器上创建一致性备份：

```bash
docker exec binhu-mysql sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" \
  --single-transaction --routines --triggers \
  --default-character-set=utf8mb4 \
  OnlineData OnlineDataArchive daily_report' \
  > "/root/binhu_backup_$(date +%Y%m%d_%H%M%S).sql"
```

备份后至少验证文件存在、大小大于零，并检查转储开头；重要变更前还应复制到服务器外的受控存储。

恢复属于高风险操作，执行前必须：

- 明确备份文件、目标服务器和目标数据库。
- 再做一次当前状态备份。
- 停止会写数据库的应用或进入维护窗口。
- 先在隔离数据库验证转储可读。
- 获得用户对覆盖范围的明确确认。
- 恢复后检查库表数量、关键记录、登录、同步和日报查询。

禁止把 `docker compose down -v` 作为恢复或排障捷径。

_最后核对：2026-07-26_
