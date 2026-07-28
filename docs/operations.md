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

## 走访明细导入

“数据工作台 → 走访汇总”支持管理员和超级管理员分别上传走访明细、星级评定 `.xlsx`：

- 单个文件不超过 20MB，最多处理 10 万条有效记录。
- 页面会显示现有数据的起止日期和无数据日期，上传重叠区间不会重复增加记录。
- 去重按“业务日期＋标准化地址＋网格员姓名”判断；同一网格员同日同地址只保留最新一条，不同网格员分别保留。
- 错误行会跳过，其他正确行继续入库；上传完成后可查看错误和提醒。
- 组长和组员只能查看数据范围，后端也会拒绝他们上传。
- 社区管理可以为正式社区维护多个别名。走访来源中的“社区”或“村”后缀会自动去掉，别名匹配成功后统一保存正式社区名称。
- 网格员跨社区走访属于正常情况，不会因为所属社区与走访社区不同而产生提醒。
- 星级评定按标准化地址关联采集时间前后 24 小时内最接近的走访，只更新 `t_visit_details`，不建立第二张星级业务表。
- 无法匹配或时间距离完全相同而无法判断的评定不会强行写入；页面会显示提醒。
- 页面会显示已星级评定数量，以及“仅入户走访、没有星级评定”的数量。
- 页面可以按入户业务日期选择一天或一段时间，同时查看网格员汇总和社区汇总。
- 汇总中的社区按本次实际走访社区统计；同一网格员跨社区走访时会分成多行。
- 社区表的人均走访户数和人均变动数，按区间内在该社区实际参与走访的不同网格员人数计算，
  均四舍五入保留 1 位小数；总计会对跨社区人员去重后重新计算。
- 网格员表、社区表和表底总计使用同一套后端计算，后续 XLSX 导出应直接复用这份结果。

这一功能新增了三张表，并给网格员资料增加身份证号字段。第一阶段已于 2026-07-28
完成上线，上线时未导入测试走访数据。以后部署相关改动时仍要检查：

1. 确认待部署版本，并完成三个数据库的有效备份。
2. 检查 `backend/init.sql` 和 `backend/database.py` 中的结构一致。
3. 更新后端依赖并重建镜像，再确认新表和字段已经创建。
4. 使用自动化测试验证上传、重叠去重、权限和身份证号遮盖，不向生产库导入测试业务数据。
5. 检查原有业务表数量、登录、同步、日报和日志没有异常。

走访汇总已经接入；XLSX 导出仍待后续接入。

## 部署服务器前

生产服务器已经在 2026-07-28 完成 HTTPS、端口收紧、凭据轮换和运维中心上线。
仍然要先看 [风险清单](known-risks.md)，尤其不要忽略 OAuth 明文和异地备份问题。

用户已经授权正常代码、前端更新和兼容性数据库变更默认同步到服务器。改动检查通过并合并到 `main`
后，除非用户明确说“不同步”“只改本地”或“先不要部署”，否则继续完成部署。

兼容性数据库变更是指只新增表或可空字段，而且旧版本仍能继续运行。部署前必须通过测试、
完成三库备份并准备回退办法。删除或覆盖数据、恢复备份、删除/重建表和数据卷、不可逆迁移、
轮换凭据等高风险操作仍要单独确认。

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

生产环境由 nginx 统一提供 HTTPS，MySQL 和 Uvicorn 不能直接响应公网请求。
当前端口关系是：

- 滨湖平台使用 nginx 的 HTTPS 443。
- 后端只绑定 `127.0.0.1:37125`，由 nginx 转发。
- MySQL 不发布宿主机端口，只在 Compose 内部使用 `mysql:3306`。

当前服务器的 HTTP 80 还承载另一套独立应用，滨湖平台没有接管或重定向它。不能直接用
仓库里的默认 80 配置覆盖服务器现有站点；更新 nginx 时要保留 ACME 验证目录和原站点。

服务器没有域名时，可以使用支持 IP 地址的短期证书。部署时先用 Certbot 5.4 或更高版本
申请证书，再把实际证书软链接到：

```text
/etc/nginx/ssl/binhu/fullchain.pem
/etc/nginx/ssl/binhu/privkey.pem
```

IP 地址证书只有约 6 天有效期。生产服务器使用 `binhu-certbot-renew.timer` 每天检查两次，
续期成功后执行 `nginx -t` 和重新加载。日常检查：

1. 80 端口的 ACME 验证目录可以访问。
2. `nginx -t` 通过。
3. HTTPS 健康接口和登录都正常。
4. `systemctl is-active binhu-certbot-renew.timer` 返回 `active`。
5. 从外部确认 `443` 可连接，`3306` 和 `37125` 不可连接。

续期镜像和 systemd 模板保存在 `nginx/`。如果本地 Certbot 镜像被清理，可以重新构建：

```bash
docker build -f nginx/Dockerfile.certbot -t binhu-certbot:5.4.0 nginx
```

安装或更新定时器前，先比较服务器现有 unit 文件并备份，不能直接覆盖未知配置。

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

## 运维中心和每日备份

运维中心已在 2026-07-28 上线。超级管理员可以从侧栏直接打开“运维中心”：

- “运行概况”查看容器、磁盘、MySQL、最近同步、最近备份和 OAuth 过期状态。
- “系统日志”查看后端和 MySQL 日志。这里只能查看，不能输入命令。
- “数据库”查看三个数据库的大小、估算行数、表结构和索引，不能修改数据。
- “备份管理”设置每日备份时间、立即创建备份和下载成功备份。
- “操作记录”查看超管执行过的重要操作。
- “导出诊断包”下载脱敏后的状态和近期错误日志，不包含业务数据。

每日备份默认按“系统设置”的时区在凌晨 2 点执行。自动和手动备份都包含
`OnlineData`、`OnlineDataArchive` 和 `daily_report`，保存 7 天，并始终保留最近一份成功备份。

下载整库备份前需要重新输入当前超级管理员密码。运维中心不提供备份删除和数据库恢复。
恢复仍然是高风险操作，必须按照上一节的规则单独确认。

生产 `.env` 必须另外提供两个值：

```dotenv
BINHU_OPS_AGENT_TOKEN=<单独生成的随机内部令牌>
BINHU_BACKUP_DIR=<服务器上的专用备份目录>
```

内部令牌不能和数据库、SSH、管理员或应用密钥共用。备份目录只用于平台备份，不要指向项目根目录或数据卷。

上线时已经完成：

1. `binhu-ops-agent` 没有发布公网端口，只有它挂载 Docker Socket。
2. 普通管理员访问运维接口返回 403；其余角色由同一后端权限规则和自动化测试覆盖。
3. 日志里出现的密码、令牌、Cookie 和 Authorization 已被遮盖。
4. 手动备份成功，下载前会重新验证密码，下载文件能通过 gzip 和 SHA-256 检查。
5. `_backup_schedule`、`_backup_jobs` 和 `_admin_audit_log` 已创建。
6. 日志流、数据库概况、操作记录和诊断包已实际请求成功。

还需要观察下一次真实的凌晨 2 点任务，确认自动备份按时完成且不会重复创建。不要在生产
环境测试恢复、删除、容器重启或故意制造数据库故障。

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
