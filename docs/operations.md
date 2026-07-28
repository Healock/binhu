# 开发与运维手册

## 最常用：两台电脑怎么开发

记住三句话：

1. 开工前先拉取。
2. 换电脑前先提交并推送。
3. 功能完成后才合并到 `main`。

### 开始一个新功能

不管在家里还是单位，都先更新正式代码：

```powershell
git switch main
git pull --ff-only
```

然后建立一个功能分支：

```powershell
$branchName = "feat/example-topic"
git switch -c $branchName
git push -u origin $branchName
```

把 `example-topic` 换成简短的功能名称。不要建立“家里分支”或“单位分支”。

### 准备换电脑

离开当前电脑前，把进度保存到 GitHub：

```powershell
git status
git add -- path/to/reviewed-file
git commit -m "开发进度：简单说明做了什么"
git push
```

功能没写完也可以提交到功能分支，只要暂时不合并进 `main`。

### 在另一台电脑继续

这台电脑第一次使用该分支：

```powershell
git fetch origin
git switch --track origin/feat/example-topic
```

这台电脑已经有该分支：

```powershell
git switch feat/example-topic
git pull --ff-only
```

### 功能完成

检查通过后推送最后一次修改，在 GitHub 创建 Pull Request，然后合并到 `main`。

合并后，两台电脑分别运行：

```powershell
git switch main
git pull --ff-only
```

`.env`、`backend/.env` 和 `AGENTS.local.md` 不会通过 GitHub 同步，两台电脑要分别配置。

## 第一次配置电脑

复制项目配置模板：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`，把所有 `replace-with-*` 示例值换成本机使用的值。不要把真实密码发到聊天或提交到 Git。

如果使用 Docker，可以检查配置：

```powershell
docker compose config --quiet
```

如果直接启动后端，还要在 `backend/` 中建立自己的 `.env`。字段示例：

```dotenv
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=binhu
MYSQL_PASSWORD=<本机数据库密码>
MYSQL_ONLINE_DATA_DB=OnlineData
MYSQL_ARCHIVE_DB=OnlineDataArchive
MYSQL_DAILY_REPORT_DB=daily_report
ENCRYPTION_KEY=<本机应用密钥>
```

## 本地启动

前端：

```powershell
Set-Location frontend
npm install
npm.cmd run dev
```

前端修改完成后检查生产构建：

```powershell
npm.cmd run build
```

后端：

```powershell
Set-Location backend
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

后端启动前需要三个可用的 MySQL 数据库，具体用途见 [架构说明](architecture.md)。

## 部署服务器前

目前服务器还有一些安全问题没有处理，不能把现有 Compose 和 nginx 配置直接当成最终部署方案。先看 [风险清单](known-risks.md)。

用户已经授权普通代码和前端更新默认同步到服务器。改动检查通过并合并到 `main`
后，除非用户明确说“不同步”“只改本地”或“先不要部署”，否则继续完成部署。

这项默认授权不包括删除数据、恢复备份、修改数据库结构、轮换凭据等高风险操作；
这些操作仍要单独确认。

每次部署前必须确认：

1. 要部署的是哪个版本。
2. 前端构建和后端检查已经通过。
3. 三个数据库已经备份，而且备份文件不是空的。
4. 服务器上的 `.env` 和数据卷不会被覆盖。
5. 出问题时知道怎么退回旧版本。

部署后要检查：

1. 健康接口能否访问。
2. 容器是否正常运行。
3. 日志是否有报错。
4. 登录和退出是否正常。
5. 同步、统计和关键数据是否正常。

## 安全入口怎么部署

生产环境必须由 nginx 统一提供 HTTPS，不能继续让 MySQL 或 Uvicorn 直接响应公网请求。
仓库中的目标端口关系是：

- 公网只开放 nginx 的 80 和 443。
- 后端只绑定 `127.0.0.1:37125`，由 nginx 转发。
- MySQL 不发布宿主机端口，只在 Compose 内部使用 `mysql:3306`。

服务器没有域名时，可以使用支持 IP 地址的短期证书。部署时先用 Certbot 5.4 或更高版本
申请证书，再把实际证书软链接到：

```text
/etc/nginx/ssl/binhu/fullchain.pem
/etc/nginx/ssl/binhu/privkey.pem
```

证书有效期较短，必须配置自动续期、续期后的 `nginx -t` 和重新加载。首次切换前先检查：

1. 80 端口的 ACME 验证目录可以访问。
2. `nginx -t` 通过。
3. HTTPS 健康接口和登录都正常。
4. 再关闭公网 37125 和 3306，不能先切断当前可用入口。
5. 从外部网络确认只有 80、443 和必要的 SSH 端口可以连接。

生产 `.env` 还要设置：

```dotenv
BINHU_SESSION_COOKIE_SECURE=true
BINHU_CORS_ALLOWED_ORIGINS=https://<公网地址>
```

新建空数据库时，临时设置 `BINHU_BOOTSTRAP_ADMIN_USERNAME` 和
`BINHU_BOOTSTRAP_ADMIN_PASSWORD`。首个超级管理员创建并成功登录后立即删除这两个变量。
已有用户的生产数据库不需要设置。

## 数据库备份与恢复

下面是给负责运维的人使用的备份命令。执行前仍要确认服务器、容器和备份目录是否正确：

```bash
docker exec binhu-mysql sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" \
  --single-transaction --routines --triggers \
  --default-character-set=utf8mb4 \
  --databases OnlineData OnlineDataArchive daily_report' \
  > "/root/binhu_backup_$(date +%Y%m%d_%H%M%S).sql"
```

`--databases` 不能省略。省略后，`mysqldump` 会把第二、第三个数据库名称误当成表名，三库备份会失败。

备份后至少检查：

- 文件存在。
- 文件大小不是 0。
- 文件开头是正常的 SQL 转储内容。
- 重要操作前，备份已经复制到服务器之外的安全位置。

恢复会覆盖数据，不能自动执行。恢复前必须再次备份当前数据，并得到用户对目标服务器、备份文件和覆盖范围的明确确认。

不要使用 `docker compose down -v` 排错或恢复数据，它会删除数据卷。

## 定时同步怎么使用和排查

超级管理员可以在“设置 → 系统设置”中开启、关闭或修改同步间隔。修改间隔或重新开启后，
倒计时会从保存时刻重新开始。常用间隔可以直接选择，自定义值必须在 5 分钟到 7 天之间。

管理员和超级管理员可以点击“立即同步”。组长和组员只能看到状态和下一次倒计时，后端也会
拒绝他们发起同步，不能只依赖前端按钮隐藏。

同步状态分成四步：等待、同步在线数据、生成报表、完成。页面显示的步骤数来自真实任务进度，
不会再显示估算的假进度。自动同步失败或部分失败后，超级管理员可以从页面上的铃铛查看站内通知。

如果自动同步没有按时开始，按下面的顺序检查：

1. 查看“系统设置”里是否开启，以及下一次执行时间是否合理。
2. 查看最新同步状态，确认没有另一项任务仍处于等待中或运行中。
3. 查看后端日志中是否有 `[SCHEDULER]` 或 `[SYNC]` 报错。
4. 只读检查 `_sync_schedule` 和最新 `_sync_log`，不要直接修改时间或任务状态。
5. 如果刚发生后端重启，确认中断任务已经被标为失败，并且下一次时间已重新安排。

部署包含定时同步改动的版本后，还要额外检查：

1. `_sync_schedule`、`_notifications` 和 `_sync_log` 新字段已经存在。
2. 使用普通管理员、组长和组员验证手动同步权限。
3. 实际等待一轮定时同步，确认来源显示为“自动触发”。
4. 确认分汇总表全部成功后，总汇总表才最后更新。
5. 不要在生产环境故意制造失败；失败通知使用自动化测试验证。

## 部署脚本使用的变量

`upload.py` 等脚本从 `BINHU_SSH_*` 环境变量读取服务器信息，例如：

```powershell
$env:BINHU_SSH_HOST = "<服务器地址>"
$env:BINHU_SSH_PORT = "<SSH端口>"
$env:BINHU_SSH_USER = "<SSH用户>"
$env:BINHU_SSH_PASSWORD = "<SSH密码>"
python upload.py
```

这里只能保留示例。真实值由每台电脑自己保存，不能写进脚本或共享文档。

_最后核对：2026-07-28_
