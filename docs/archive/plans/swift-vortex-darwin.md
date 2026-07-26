# 滨湖智慧平台 / binhu 重命名实施计划

> [!WARNING]
> 历史证据，不可直接执行。内容可能与当前源码、配置和运行环境不一致；现行规则见 [文档索引](../../README.md) 与 [风险登记](../../known-risks.md)。

> 范围:C 彻底全改(含 MySQL 库名/用户名/密码、加密密钥、远程目录路径、容器名)
> 本地目录 `仓库根目录` 保持不动,只改内容
> 远程目录 `/root/bhzh` → `/root/binhu`

---

## 0. 探索阶段的关键发现(修正用户的风险预设)

在动手前必须先纠正用户清单中三个对系统行为的误解,它们直接决定方案选型:

### 发现 1:ENCRYPTION_KEY 是"死配置",改它零风险
- `backend/config.py` 定义了 `ENCRYPTION_KEY`,但**全 backend 没有任何代码读取它**。
- Grep 搜索 `encrypt|decrypt|Fernet|AES|cryptography` 在 backend 仅命中 3 处:
  - `config.py` — 配置定义(无人引用)
  - `requirements.txt` — `cryptography==44.0.0` 包声明(代码中无 import)
  - `auth.py` — 注释 `# 简单的加密存储(生产环境应使用 AES)`
- `backend/routers/auth.py` 的 `save_oauth` 实际是**明文 INSERT**:
  ```python
  await cur.execute(
      """INSERT INTO oauth_tokens (...) VALUES (%s, %s, %s, %s, %s, %s)""",
      (data.client_id, data.client_secret, data.access_token, ...)
  )
  ```
- `backend/services/sync_engine.py` 的 `_get_oauth_creds` 也是**明文 SELECT** 直接用。

**结论**: 用户担心的"选项A/B/C 解密重加密"是伪问题。OAuth 凭据本来就是明文存,改 ENCRYPTION_KEY 字符串不会破坏任何数据。**推荐直接改字符串 `$BINHU_ENCRYPTION_KEY` → `$BINHU_ENCRYPTION_KEY`**,无需任何数据迁移步骤。`cryptography` 包和 `ENCRYPTION_KEY` 配置保留(作为占位,无害)。

### 发现 2:Docker volume 名会随 compose 项目名变化,数据不能"原地保留"
- `docker-compose.yml` 用的是命名卷 `mysql_data:/var/lib/mysql`。
- Docker Compose 的命名卷实际名 = `<项目名>_mysql_data`,而**项目名默认取自所在目录名**。
- 当前远程目录 `/root/bhzh` → volume 实际名是 `bhzh_mysql_data`。
- 改名为 `/root/binhu` 后,新容器启动会创建 `binhu_mysql_data`(空卷),旧卷 `bhzh_mysql_data` 的数据**不会自动迁移**。
- 又因 MySQL 库名(`quanliantiao`→`binhu`)、用户名、密码都要改,这些是 MySQL 数据字典里的内容,即使把旧 volume 挂回新容器,权限也对不上。

**结论**: 不能复用旧 volume。必须走 **mysqldump 全量导出 → 重建容器 → 导入新库** 的干净路径。旧 volume 保留做回滚保险。

### 发现 3:数据库表名/字段名与项目名无关
- `backend/database.py` 和 `backend/init.sql` 的表名全是英文(`spreadsheets`、`raw_data`、`daily_stats`、`oauth_tokens`、`sync_log`),字段名是中文业务术语(`核查人`、`下发日期` 等)。改库名不影响表结构,只需 dump → 改库 → 导入。

---

## 1. 执行顺序总览(考虑依赖关系)

```
阶段 A:本地代码改动(无副作用,可反复修改)
  └─ 改完后本地不需要跑(npm run build 在服务器或本地都可)

阶段 B:服务器侧 — 停服窗口开始
  B1. 旧容器还在运行时,mysqldump 全量备份 quanliantiao 库(关键!这是数据保险)
  B2. 停旧容器 (cd /root/bhzh && docker compose down)
  B3. 备份旧 volume 到 tar 包(双保险)
  B4. 备份旧 nginx 配置 /etc/nginx/conf.d/bhzh.conf

阶段 C:服务器侧 — 目录改名 + 文件替换
  C1. mv /root/bhzh /root/binhu
  C2. 上传本地改好的文件覆盖 /root/binhu 对应文件
      (docker-compose.yml / nginx/binhu.conf / backend/* / frontend/dist/* / *.py 脚本)
  C3. 删除旧 nginx 配置 /etc/nginx/conf.d/bhzh.conf
  C4. 部署新配置 cp /root/binhu/nginx/binhu.conf /etc/nginx/conf.d/binhu.conf
  C5. nginx -t 验证

阶段 D:服务器侧 — 启动新容器 + 数据导入
  D1. cd /root/binhu && docker compose up -d --build
      (新容器首次启动,MYSQL_DATABASE=binhu 自动建库 + 建用户 + 跑 init.sql)
  D2. 等 MySQL healthy
  D3. 导入数据:docker exec -i binhu-mysql mysql -uroot -p$BINHU_MYSQL_ROOT_PASSWORD binhu < backup.sql

阶段 E:服务器侧 — 收尾
  E1. nginx -s reload
  E2. 跑验证清单
  E3. (验证通过 24h 后)清理:docker volume rm bhzh_mysql_data、删除 bhzh 目录备份

阶段 F:回滚(仅在验证失败时)
```

**关键依赖**:B1(数据备份)必须在 B2(停容器)之前;D1(新容器启动建库)必须在 D3(导入数据)之前;C1(改名)必须在 C2(上传覆盖)之前。

---

## 2. 数据库迁移方案(MySQL 不支持 RENAME DATABASE)

### 2.1 导出(旧容器还活着时)

```bash
# 在服务器上执行,旧容器名为 quanliantiao-mysql
docker exec quanliantiao-mysql \
  sh -c 'mysqldump -uroot -p$BINHU_MYSQL_ROOT_PASSWORD \
    --single-transaction \
    --routines \
    --triggers \
    --default-character-set=utf8mb4 \
    quanliantiao' > /root/quanliantiao_backup_$(date +%Y%m%d_%H%M%S).sql

# 校验备份
ls -lh /root/quanliantiao_backup_*.sql
head -20 /root/quanliantiao_backup_*.sql   # 确认有 CREATE TABLE
grep -c "INSERT INTO" /root/quanliantiao_backup_*.sql   # 数 INSERT 条数
```

**注意**: 不加 `--databases` 参数,这样导出文件不含 `CREATE DATABASE` 和 `USE` 语句,导入时可以自由指定目标库 `binhu`。

### 2.2 volume 双保险备份(可选,但强烈建议)

```bash
# 把整个 mysql_data volume 打包,以防 mysqldump 也有问题
docker run --rm \
  -v bhzh_mysql_data:/source:ro \
  -v /root:/backup \
  alpine tar czf /backup/volume_bhzh_$(date +%Y%m%d).tar.gz -C /source .
```

### 2.3 新库创建(由 docker-compose 自动完成)

新 `docker-compose.yml` 里 `MYSQL_DATABASE: binhu` + `MYSQL_USER: binhu` + `MYSQL_PASSWORD: $BINHU_MYSQL_PASSWORD`,容器首次启动会:
1. 自动 `CREATE DATABASE binhu CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;`
2. 自动 `CREATE USER 'binhu'@'%' IDENTIFIED BY '$BINHU_MYSQL_PASSWORD'; GRANT ALL ON binhu.* TO 'binhu'@'%';`
3. 因为 volume 是空的,会执行 `/docker-entrypoint-initdb.d/init.sql` 建表

所以**无需手动建库建表**,容器启动即就绪。

### 2.4 导入数据

```bash
# 等 binhu-mysql healthy 后
docker exec -i binhu-mysql \
  mysql -uroot -p$BINHU_MYSQL_ROOT_PASSWORD --default-character-set=utf8mb4 binhu \
  < /root/quanliantiao_backup_*.sql

# 校验数据
docker exec binhu-mysql mysql -uroot -p$BINHU_MYSQL_ROOT_PASSWORD -e "USE binhu; SHOW TABLES; SELECT COUNT(*) FROM spreadsheets; SELECT COUNT(*) FROM raw_data; SELECT COUNT(*) FROM oauth_tokens;"
```

### 2.5 root 密码注意

`docker-compose.yml` `MYSQL_ROOT_PASSWORD: $BINHU_MYSQL_ROOT_PASSWORD` 保持不变(用户清单未要求改 root 密码,且 root 只在容器内本地连接,改了反而麻烦)。如果你也想改 root 密码,需在 docker-compose.yml 同步修改,并在 2.1/2.4 的命令里替换。**本计划不改 root 密码**。

---

## 3. 加密密钥处理方案

**推荐: 直接改字符串,无数据迁移。**

理由(见 0.发现1):
- `ENCRYPTION_KEY` 在代码里没有任何 import 或使用。
- OAuth 凭据在 `oauth_tokens` 表是明文存储。
- 改这个字符串只是改一个配置值,对运行时行为零影响。

**改动**:
- `backend/config.py`: `ENCRYPTION_KEY: str = "$BINHU_ENCRYPTION_KEY"` → `ENCRYPTION_KEY: str = "$BINHU_ENCRYPTION_KEY"`
- `docker-compose.yml`: `ENCRYPTION_KEY: $BINHU_ENCRYPTION_KEY` → `ENCRYPTION_KEY: $BINHU_ENCRYPTION_KEY`

`cryptography==44.0.0` 在 `requirements.txt` 保留不动(无 import,无害;删了会改动依赖文件反而不必要)。

---

## 4. 文件修改清单(历史 old → new)

> 路径全部基于本地 `仓库根目录\`
> 远程对应路径见部署章节

### 4.1 第一层 bhzh 路径(14 处 + 1 文件名)

**文件 1: `仓库根目录\upload.py`**
- `REMOTE_BASE = "/root/bhzh"` → `REMOTE_BASE = "/root/binhu"`
- `LOCAL_BASE = r"旧仓库根目录"` → `LOCAL_BASE = r"仓库根目录"`
  (此行同时属于第三层:消除了"全链条汇总工具"字符串,并修正了已过时的本地路径——实际项目在 Desktop\Bhzh 而非 WorkBuddy\全链条汇总工具)

**文件 2: `仓库根目录\setup_server.py`**
- `cp /root/bhzh/nginx/bhzh.conf /etc/nginx/conf.d/bhzh.conf` → `cp /root/binhu/nginx/binhu.conf /etc/nginx/conf.d/binhu.conf`
- `cd /root/bhzh && docker compose pull 2>&1 || true` → `cd /root/binhu && docker compose pull 2>&1 || true`
- `cd /root/bhzh && docker compose up -d --build 2>&1` → `cd /root/binhu && docker compose up -d --build 2>&1`
- `cd /root/bhzh && docker compose ps` → `cd /root/binhu && docker compose ps`
- `docker inspect quanliantiao-mysql` → `docker inspect binhu-mysql`(此行属第二层,在此文件一并改)
- `docker logs quanliantiao-backend` → `docker logs binhu-backend`(此行属第二层)

**文件 3: `仓库根目录\retry_deploy.py`**
- `# Try to add bhzh.conf to the right place` → `# Try to add binhu.conf to the right place`
- `cp /root/bhzh/nginx/bhzh.conf {nginx_conf_dir}bhzh.conf` → `cp /root/binhu/nginx/binhu.conf {nginx_conf_dir}binhu.conf`
- `# If bhzh.conf has server_name _, ...` → `# If binhu.conf has server_name _, ...`
- `if [ "$f" != "{nginx_conf_dir}bhzh.conf" ]; then` → `if [ "$f" != "{nginx_conf_dir}binhu.conf" ]; then`
- `cat /root/bhzh/nginx/bhzh.conf` → `cat /root/binhu/nginx/binhu.conf`
- `cd /root/bhzh` → `cd /root/binhu`
- `docker rm -f quanliantiao-mysql quanliantiao-backend` → `docker rm -f binhu-mysql binhu-backend`(此行属第二层)
- `cd /root/bhzh && docker compose build backend` → `cd /root/binhu && docker compose build backend`
- `cd /root/bhzh && docker compose up -d` → `cd /root/binhu && docker compose up -d`
- `docker inspect quanliantiao-mysql` → `docker inspect binhu-mysql`(此行属第二层)
- `docker logs quanliantiao-mysql --tail 20` → `docker logs binhu-mysql --tail 20`(此行属第二层)
- `docker logs quanliantiao-mysql --tail 30` → `docker logs binhu-mysql --tail 30`(此行属第二层)
- `docker logs quanliantiao-backend --tail 30` → `docker logs binhu-backend --tail 30`(此行属第二层)

**文件 4: `仓库根目录\nginx\bhzh.conf`**
- `root /root/bhzh/frontend/dist;` → `root /root/binhu/frontend/dist;`
- **文件名本身改名**: `nginx/bhzh.conf` → `nginx/binhu.conf`(需要本地 mv + 远程 mv)

### 4.2 第二层 quanliantiao(全部出处,共 23 处跨 6 文件)

**文件 5: `仓库根目录\docker-compose.yml`**
- `container_name: quanliantiao-mysql` → `container_name: binhu-mysql`
- `MYSQL_DATABASE: quanliantiao` → `MYSQL_DATABASE: binhu`
- `MYSQL_USER: quanliantiao` → `MYSQL_USER: binhu`
- `MYSQL_PASSWORD: $BINHU_MYSQL_PASSWORD` → `MYSQL_PASSWORD: $BINHU_MYSQL_PASSWORD`
- `container_name: quanliantiao-backend` → `container_name: binhu-backend`
- `MYSQL_USER: quanliantiao`(backend env) → `MYSQL_USER: binhu`
- `MYSQL_PASSWORD: $BINHU_MYSQL_PASSWORD`(backend env) → `MYSQL_PASSWORD: $BINHU_MYSQL_PASSWORD`
- `MYSQL_DATABASE: quanliantiao`(backend env) → `MYSQL_DATABASE: binhu`
- `ENCRYPTION_KEY: $BINHU_ENCRYPTION_KEY` → `ENCRYPTION_KEY: $BINHU_ENCRYPTION_KEY`

**文件 6: `仓库根目录\backend\config.py`**
- `MYSQL_USER: str = "quanliantiao"` → `MYSQL_USER: str = "binhu"`
- `MYSQL_PASSWORD: str = "$BINHU_MYSQL_PASSWORD"` → `MYSQL_PASSWORD: str = "$BINHU_MYSQL_PASSWORD"`
- `MYSQL_DATABASE: str = "quanliantiao"` → `MYSQL_DATABASE: str = "binhu"`
- `ENCRYPTION_KEY: str = "$BINHU_ENCRYPTION_KEY"` → `ENCRYPTION_KEY: str = "$BINHU_ENCRYPTION_KEY"`

**文件 7/8: setup_server.py 和 retry_deploy.py** — 已在 4.1 列出(setup_server.py;retry_deploy.py)

**文件 9: `仓库根目录\frontend\package.json`**
- `"name": "quanliantiao-frontend",` → `"name": "binhu-frontend",`

**文件 10: `仓库根目录\frontend\package-lock.json`**
- `"name": "quanliantiao-frontend",` → `"name": "binhu-frontend",`
- `"name": "quanliantiao-frontend",`(packages[""] 节点内) → `"name": "binhu-frontend",`
- **不需要跑 `npm install`**:package-lock.json 里这两处只是顶层包名元数据(name 字段),不影响依赖树解析。手动改完直接 `npm run build` 即可。若想完全干净,改完 `npm install` 重新生成 lock 也行,但非必需。

### 4.3 第三层 "全链条汇总工具"(源文件 7 处)

**文件 11: `仓库根目录\backend\main.py`**
- `"""全链条汇总工具 - FastAPI 入口"""` → `"""滨湖智慧平台 - FastAPI 入口"""`
- `title="全链条汇总工具",` → `title="滨湖智慧平台",`
- `description="从腾讯文档获取全链条数据，统计核查结果，生成数据透视表",` → `description="从腾讯文档获取数据，统计核查结果，生成数据透视表",`(去掉"全链条"业务前缀,改成中性表述)
- `return {"status": "ok", "message": "全链条汇总工具运行中"}` → `return {"status": "ok", "message": "滨湖智慧平台运行中"}`

**文件 12: `仓库根目录\backend\init.sql`**
- `-- 全链条汇总工具 - 数据库初始化脚本` → `-- 滨湖智慧平台 - 数据库初始化脚本`

**文件 13: `仓库根目录\backend\schemas\spreadsheet.py`**
- `data_sheet_id: str = Field(default="000001", description="全链条数据所在子表ID")` → `data_sheet_id: str = Field(default="000001", description="数据所在子表ID")`

**文件 14: `仓库根目录\upload.py`** — 对应位置 已在 4.1 列出(消除"全链条汇总工具")

**文件 15: `仓库根目录\retry_deploy.py`**
- `print("  全链条汇总工具 - 重新部署")` → `print("  滨湖智慧平台 - 重新部署")`

**文件 16: `仓库根目录\frontend\index.html`**
- `<title>全链条汇总工具</title>` → `<title>滨湖智慧平台</title>`

**文件 17: `仓库根目录\frontend\src\components\Layout.tsx`**
- `<h1 className="text-lg font-bold text-gray-800">全链条汇总工具</h1>` → `<h1 className="text-lg font-bold text-gray-800">滨湖智慧平台</h1>`

**构建产物(自动重生成,不手改)**:
- `frontend/dist/index.html`
- `frontend/dist/assets/index-*.js:67`

改完源文件后,在本地或服务器执行 `cd frontend && npm run build`,dist/ 自动更新。

### 4.4 不需要改的文件(已确认无相关字符串)

- `backend/Dockerfile` — 无项目名引用
- `backend/database.py` — 表名是英文,字段是中文业务术语,与项目名无关
- `backend/models/orm.py` — 同上
- `backend/services/*.py` — 无项目名引用
- `backend/routers/auth.py`、`sync.py`、`stats.py`、`spreadsheets.py` — 无项目名引用
- `backend/requirements.txt` — `cryptography==44.0.0` 保留(无用但无害)
- `fix_server.py` — 无项目名引用(用户清单未提及,跳过)
- `.env` — 不存在(Glob 未找到)
- `backend/.env`、`backend/Dockerfile` — 无引用

---

## 5. 部署步骤(本地 → 服务器)

### 阶段 A:本地代码改动(在 `仓库根目录` 下完成第 4 节所有修改)

1. 按第 4 节清单逐文件修改 17 个文件
2. 把 `nginx/bhzh.conf` 重命名为 `nginx/binhu.conf`
3. 在 `frontend/` 目录下执行 `npm run build` 重新生成 `frontend/dist/`
   - 若本地未装 node_modules,可跳过,在服务器上 build;但因为 upload.py 的 UPLOAD_ITEMS 包含 `frontend/dist`,本地 build 好再上传更稳

### 阶段 B:服务器侧 — 停服窗口开始(SSH 到 $BINHU_SSH_HOST:$BINHU_SSH_PORT)

```bash
# B1. 关键:旧容器还活着时导出数据库(数据保险)
docker exec quanliantiao-mysql \
  sh -c 'mysqldump -uroot -p$BINHU_MYSQL_ROOT_PASSWORD --single-transaction --routines --triggers --default-character-set=utf8mb4 quanliantiao' \
  > /root/quanliantiao_backup_$(date +%Y%m%d_%H%M%S).sql

# 校验备份文件非空且有内容
ls -lh /root/quanliantiao_backup_*.sql
grep -c "INSERT INTO" /root/quanliantiao_backup_*.sql

# B2. volume 双保险
docker run --rm -v bhzh_mysql_data:/source:ro -v /root:/backup \
  alpine tar czf /backup/volume_bhzh_$(date +%Y%m%d).tar.gz -C /source .

# B3. 备份旧 nginx 配置
cp /etc/nginx/conf.d/bhzh.conf /root/bhzh.conf.bak.$(date +%Y%m%d)

# B4. 停旧容器
cd /root/bhzh && docker compose down
```

### 阶段 C:服务器侧 — 目录改名 + 文件替换

```bash
# C1. 远程目录改名
mv /root/bhzh /root/binhu

# C2. 本地用 upload.py 或 scp 上传覆盖(本地 cd 到 Desktop\Bhzh 跑)
#    upload.py 已改好 REMOTE_BASE="/root/binhu" 和 LOCAL_BASE
#    注意 upload.py 的 UPLOAD_ITEMS 会覆盖:
#      - docker-compose.yml
#      - backend/  (整个目录,含 config.py / main.py / init.sql / schemas 等)
#      - frontend/dist  (重新 build 的产物)
#      - nginx/  (含改名后的 binhu.conf)
#    在本地执行:python upload.py

# C3. 清理旧 nginx 配置
rm -f /etc/nginx/conf.d/bhzh.conf

# C4. 部署新 nginx 配置
cp /root/binhu/nginx/binhu.conf /etc/nginx/conf.d/binhu.conf

# C5. 验证 nginx 配置
nginx -t
```

### 阶段 D:服务器侧 — 启动新容器 + 数据导入

```bash
# D1. 启动新容器(首次启动会自动建 binhu 库 + binhu 用户 + 跑 init.sql 建表)
cd /root/binhu && docker compose up -d --build

# D2. 等 MySQL healthy(最多 60s)
for i in $(seq 1 12); do
  sleep 5
  status=$(docker inspect binhu-mysql --format='{{.State.Health.Status}}' 2>/dev/null)
  echo "[$i/12] MySQL: $status"
  [ "$status" = "healthy" ] && break
done

# D3. 导入数据到新库 binhu
docker exec -i binhu-mysql \
  mysql -uroot -p$BINHU_MYSQL_ROOT_PASSWORD --default-character-set=utf8mb4 binhu \
  < /root/quanliantiao_backup_*.sql

# D4. 数据校验
docker exec binhu-mysql mysql -uroot -p$BINHU_MYSQL_ROOT_PASSWORD -e "
USE binhu;
SHOW TABLES;
SELECT 'spreadsheets' AS tbl, COUNT(*) FROM spreadsheets
UNION ALL SELECT 'raw_data', COUNT(*) FROM raw_data
UNION ALL SELECT 'daily_stats', COUNT(*) FROM daily_stats
UNION ALL SELECT 'oauth_tokens', COUNT(*) FROM oauth_tokens
UNION ALL SELECT 'sync_log', COUNT(*) FROM sync_log;
"
```

### 阶段 E:服务器侧 — 收尾

```bash
# E1. 重载 nginx
nginx -s reload

# E2. 验证(见第 7 节)

# E3. (验证通过且观察 24h 后)清理备份
# docker volume rm bhzh_mysql_data
# rm -rf /root/quanliantiao_backup_*.sql /root/volume_bhzh_*.tar.gz /root/bhzh.conf.bak.*
```

---

## 6. 回滚方案(任一阶段失败时)

### 失败点 1:阶段 D 容器起不来 / 数据导入失败

```bash
# 1. 停新容器
cd /root/binhu && docker compose down

# 2. 把目录名改回去
mv /root/binhu /root/bhzh

# 3. 恢复旧 nginx 配置
cp /root/bhzh.conf.bak.* /etc/nginx/conf.d/bhzh.conf
nginx -s reload

# 4. 旧 volume 还在(bhzh_mysql_data),旧容器直接重启即可恢复服务
cd /root/bhzh && docker compose up -d
# (旧 docker-compose.yml 已被本地改好的版本覆盖,所以这一步会失败)
```

**重要**: 阶段 C2 上传覆盖了 `/root/bhzh/docker-compose.yml`(已被改名为 /root/binhu)。若要回滚到旧 compose 配置,需在阶段 B 之前额外备份 docker-compose.yml:

```bash
# 在阶段 B 增加:
cp /root/bhzh/docker-compose.yml /root/docker-compose.yml.bak.$(date +%Y%m%d)
cp /root/bhzh/backend/config.py /root/config.py.bak.$(date +%Y%m%d)
```

回滚时:
```bash
mv /root/binhu /root/bhzh
cp /root/docker-compose.yml.bak.* /root/bhzh/docker-compose.yml
cp /root/config.py.bak.* /root/bhzh/backend/config.py
cd /root/bhzh && docker compose up -d
```

### 失败点 2:数据导入成功但应用行为异常

旧 volume `bhzh_mysql_data` 未删,数据完整。可随时:
```bash
# 用旧 volume 起一个临时 mysql 容器导出对比
docker run --rm -v bhzh_mysql_data:/var/lib/mysql mysql:8.0 \
  mysqldump -uroot -p$BINHU_MYSQL_ROOT_PASSWORD --single-transaction quanliantiao > /tmp/recheck.sql
```

---

## 7. 验证清单(逐层确认改对)

### 7.1 第一层 bhzh → binhu
```bash
# 服务器上确认无残留
grep -rn "bhzh" /root/binhu/ 2>/dev/null
grep -rn "bhzh" /etc/nginx/conf.d/
# 预期:无输出(或仅出现在 .bak 备份文件名里)
ls /root/binhu/nginx/  # 预期:binhu.conf(不是 bhzh.conf)
ls /etc/nginx/conf.d/  # 预期:binhu.conf(不是 bhzh.conf)
```

### 7.2 第二层 quanliantiao → binhu
```bash
# 服务器上确认无残留
grep -rn "quanliantiao" /root/binhu/ 2>/dev/null
# 预期:无输出

# 容器名
docker ps --format '{{.Names}}'
# 预期:binhu-mysql, binhu-backend(无 quanliantiao-*)

# MySQL 库 + 用户
docker exec binhu-mysql mysql -uroot -p$BINHU_MYSQL_ROOT_PASSWORD -e "SHOW DATABASES; SELECT user FROM mysql.user;"
# 预期:有 binhu 库,有 binhu 用户;无 quanliantiao 库和用户
```

### 7.3 第三层 "全链条汇总工具" → "滨湖智慧平台"
```bash
# 后端
grep -rn "全链条汇总工具" /root/binhu/backend/ 2>/dev/null
grep -rn "全链条汇总工具" /root/binhu/*.py 2>/dev/null
# 预期:无输出

curl -s http://localhost:8000/api/health
# 预期:{"status":"ok","message":"滨湖智慧平台运行中"}

curl -s http://localhost:8000/openapi.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['info']['title'])"
# 预期:滨湖智慧平台

# 前端
grep -rn "全链条汇总工具" /root/binhu/frontend/src/ 2>/dev/null
grep -rn "全链条汇总工具" /root/binhu/frontend/dist/ 2>/dev/null
# 预期:无输出

curl -s http://localhost/ | grep -o '<title>[^<]*</title>'
# 预期:<title>滨湖智慧平台</title>
```

### 7.4 数据完整性
```bash
docker exec binhu-mysql mysql -uroot -p$BINHU_MYSQL_ROOT_PASSWORD binhu -e "
SELECT 'spreadsheets' AS t, COUNT(*) FROM spreadsheets
UNION ALL SELECT 'raw_data', COUNT(*) FROM raw_data
UNION ALL SELECT 'daily_stats', COUNT(*) FROM daily_stats
UNION ALL SELECT 'oauth_tokens', COUNT(*) FROM oauth_tokens
UNION ALL SELECT 'sync_log', COUNT(*) FROM sync_log;
"
# 对比导入前的行数(从 /root/quanliantiao_backup_*.sql 里 grep -c "INSERT INTO" 估算)
```

### 7.5 端到端功能
```bash
# 前端首页
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/
# 预期:200

# 后端通过 nginx
curl -s http://localhost/api/health
# 预期:{"status":"ok","message":"滨湖智慧平台运行中"}

# 容器健康
docker compose ps  # 在 /root/binhu 下
# 预期:两个容器都 Up (healthy)
```

---

## 8. 关键风险与注意事项

1. **数据备份是第一优先级**:阶段 B1 的 mysqldump 必须在停容器前完成,且校验文件非空。这是整个流程的安全网。
2. **upload.py 的 LOCAL_BASE 路径修正**:原值 `旧仓库根目录` 是过时路径,改为实际的 `仓库根目录`。否则阶段 C2 上传会失败。
3. **nginx 配置文件名冲突**:阶段 C3 删除旧 bhzh.conf 后,如果 `/etc/nginx/conf.d/` 下还有其他监听 80 端口的 default 配置,会和 binhu.conf 的 `server_name _;` 冲突。retry_deploy.py 已有冲突处理逻辑,但首次部署建议手动 `ls /etc/nginx/conf.d/` 确认。
4. **MySQL root 密码不变**:本计划保持 `MYSQL_ROOT_PASSWORD: $BINHU_MYSQL_ROOT_PASSWORD` 不变。若要改,需同步修改 docker-compose.yml 和所有 mysqldump/mysql 命令。
5. **package-lock.json 改完不需要 npm install**:只是 name 元数据变更,不影响依赖解析。改完直接 `npm run build`。
6. **旧 volume 保留**:阶段 E3 的 `docker volume rm bhzh_mysql_data` 是可选清理,建议观察 24-48h 确认无问题再删。
7. **本地 Bhzh 目录名不动**:本地路径 `仓库根目录` 保持,只有 upload.py 的 LOCAL_BASE 指向它(已修正)。

---

## 9. 涉及的关键文件路径汇总(供实施时快速定位)

本地(均以仓库根目录为基准):
- `仓库根目录\docker-compose.yml`
- `仓库根目录\backend\config.py`
- `仓库根目录\backend\main.py`
- `仓库根目录\backend\init.sql`
- `仓库根目录\backend\schemas\spreadsheet.py`
- `仓库根目录\setup_server.py`
- `仓库根目录\retry_deploy.py`
- `仓库根目录\upload.py`
- `仓库根目录\nginx\bhzh.conf` → 改名为 `nginx\binhu.conf`
- `仓库根目录\frontend\package.json`
- `仓库根目录\frontend\package-lock.json`
- `仓库根目录\frontend\index.html`
- `仓库根目录\frontend\src\components\Layout.tsx`
- `仓库根目录\frontend\dist\` (由 npm run build 自动重生成)

远程(部署目标):
- `/root/bhzh` → `/root/binhu`(目录改名)
- `/etc/nginx/conf.d/bhzh.conf` → `/etc/nginx/conf.d/binhu.conf`
- Docker volume: `bhzh_mysql_data`(旧,保留) → `binhu_mysql_data`(新)
- Docker 容器:`quanliantiao-mysql/backend`(旧) → `binhu-mysql/backend`(新)
> **历史归档，禁止直接执行。** 本计划描述旧版本实现，路径、端口、变量和操作顺序可能已失效。请先阅读 `docs/README.md` 与 `docs/known-risks.md`，并重新验证当前源码和运行状态。
