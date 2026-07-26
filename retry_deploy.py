"""重新部署脚本 - 一站式修复 + 配置"""
import paramiko
import os
import time
import sys

HOST = os.getenv("BINHU_SSH_HOST")
PORT = int(os.getenv("BINHU_SSH_PORT", "22"))
USER = os.getenv("BINHU_SSH_USER", "root")
PASSWORD = os.getenv("BINHU_SSH_PASSWORD")


def run(ssh, cmd, desc="", timeout=120):
    """执行远程命令"""
    if desc:
        print(f"\n--- {desc} ---")
    # Use a channel to avoid timeout issues
    chan = ssh.get_transport().open_session()
    chan.exec_command(cmd)
    # Read output with timeout
    import select
    out_data = b""
    err_data = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if chan.recv_ready():
            out_data += chan.recv(4096)
        if chan.recv_stderr_ready():
            err_data += chan.recv_stderr(4096)
        if chan.exit_status_ready():
            break
        if not chan.recv_ready() and not chan.recv_stderr_ready():
            # Nothing to read, exit if command is done
            if chan.exit_status_ready():
                break
            time.sleep(0.5)
    # Drain remaining
    try:
        while chan.recv_ready():
            out_data += chan.recv(4096)
        while chan.recv_stderr_ready():
            err_data += chan.recv_stderr(4096)
    except:
        pass

    out = out_data.decode(errors="replace").strip()
    err = err_data.decode(errors="replace").strip()
    exit_code = chan.recv_exit_status()

    if out:
        print(out)
    if err:
        print(f"[STDERR] {err}")

    return out, err, exit_code


def main():
    if not HOST or not PASSWORD:
        raise RuntimeError("Set BINHU_SSH_HOST and BINHU_SSH_PASSWORD before redeploying.")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        print("=" * 60)
        print("  滨湖智慧平台 - 重新部署")
        print("=" * 60)

        ssh.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=15)
        print("SSH 连接成功！\n")

        # ============================================
        # 步骤1: 诊断当前状态
        # ============================================
        print("【步骤1】诊断当前状态\n")
        run(ssh, "cat /etc/os-release | head -3", "操作系统")
        run(ssh, "docker --version 2>&1; docker compose version 2>&1", "Docker 版本")

        # ============================================
        # 步骤2: 配置 Docker 国内镜像加速
        # ============================================
        print("\n【步骤2】配置 Docker 镜像加速器")

        run(ssh, """
mkdir -p /etc/docker
cat > /etc/docker/daemon.json << 'DOCKEREOF'
{
    "registry-mirrors": [
        "https://docker.1ms.run",
        "https://hub.rat.dev"
    ],
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    }
}
DOCKEREOF
systemctl daemon-reload
systemctl restart docker
sleep 2
echo "Docker 已重启"
""", "写入镜像加速器配置")

        # 验证镜像源
        out, _, _ = run(ssh, "docker info 2>&1 | grep -A5 'Registry Mirrors'", "验证镜像源")
        if "1ms.run" not in out and "rat.dev" not in out:
            print("⚠️ 镜像源配置可能未生效，尝试备用方案...")

        # 测试拉取
        print("\n尝试拉取 MySQL 镜像（可能需要几分钟）...")
        out, err, code = run(ssh, "docker pull mysql:8.0 2>&1", "拉取 MySQL 8.0", timeout=180)
        if code != 0:
            print("❌ Docker 镜像拉取失败，尝试备用镜像源...")
            # Try alternative mirrors
            run(ssh, """
cat > /etc/docker/daemon.json << 'DOCKEREOF'
{
    "registry-mirrors": [
        "https://hub-mirror.c.163.com",
        "https://mirror.baidubce.com",
        "https://docker.m.daocloud.io"
    ],
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    }
}
DOCKEREOF
systemctl restart docker
sleep 2
""", "切换备用镜像源")
            time.sleep(3)
            out, err, code = run(ssh, "docker pull mysql:8.0 2>&1", "重试拉取 MySQL", timeout=180)
            if code != 0:
                # Check if mysql image already exists
                out, _, _ = run(ssh, "docker images mysql --format '{{.Repository}}:{{.Tag}}' 2>&1", "检查已有 MySQL 镜像")
                if "mysql:8.0" not in out:
                    print("❌ 无法拉取 MySQL 镜像，尝试检查代理设置...")
                    run(ssh, "env | grep -i proxy; cat /etc/systemd/system/docker.service.d/*.conf 2>/dev/null || echo '无代理配置'", "检查代理")

        # Also try to pull python image for building backend
        out, err, code = run(ssh, "docker pull python:3.11-slim 2>&1", "拉取 Python 3.11-slim (构建后端用)", timeout=180)

        # ============================================
        # 步骤3: 查找正确的 Nginx 配置路径
        # ============================================
        print("\n【步骤3】配置 Nginx")

        out, _, _ = run(ssh, "cat /www/server/nginx/conf/nginx.conf 2>/dev/null | grep -E '^\\s*include' | head -15", "Nginx 主配置 include 路径")

        # Check common paths
        paths_to_try = [
            "/etc/nginx/conf.d/",
            "/www/server/nginx/conf/conf.d/",
            "/www/server/panel/vhost/nginx/",
            "/www/server/nginx/conf/vhost/",
        ]
        nginx_conf_dir = None
        for p in paths_to_try:
            out, _, _ = run(ssh, f"ls {p} 2>/dev/null | head -5", f"检查 {p}")
            if out:
                nginx_conf_dir = p
                print(f"  ✅ Nginx 配置目录: {p}")
                break

        if not nginx_conf_dir:
            # Try to create the directory
            nginx_conf_dir = "/etc/nginx/conf.d/"
            run(ssh, f"mkdir -p {nginx_conf_dir}", "创建 conf.d 目录")
            # Also add include to nginx.conf
            run(ssh, f"""
if grep -q "conf.d" /www/server/nginx/conf/nginx.conf 2>/dev/null; then
    echo "conf.d 已包含"
elif grep -q "include.*conf.d" /etc/nginx/nginx.conf 2>/dev/null; then
    echo "conf.d 已包含"
else
    echo "需要手动确认 Nginx include 路径"
fi
""", "检查 include")

        # Also check if server conf has existing site config
        run(ssh, f"grep -r '{HOST}' /www/server/ 2>/dev/null | head -10 || true", "查找已有站点配置")

        # Find the actual nginx binary and config
        _, _, _ = run(ssh, "which nginx; nginx -t 2>&1 | head -5", "Nginx 路径及测试结果")

        # ============================================
        # 步骤4: 部署 Nginx 配置
        # ============================================
        print("\n【步骤4】部署 Nginx 站点配置")

        # Try to add binhu.conf to the right place
        if nginx_conf_dir:
            # Note: Make sure the path doesn't use proxy_pass to lcoalhost since we use 127.0.0.1
            result = run(ssh, f"cp /root/binhu/nginx/binhu.conf {nginx_conf_dir}binhu.conf && echo 'OK'", "复制 Nginx 配置")

            # If binhu.conf has server_name _, and default.conf uses _, we need to handle conflict
            run(ssh, f"""
# Remove default conf that conflicts
rm -f {nginx_conf_dir}default.conf 2>/dev/null
rm -f /etc/nginx/sites-enabled/default 2>/dev/null
# Also check for existing configs with port 80
grep -rl 'listen.*80' {nginx_conf_dir} 2>/dev/null | while read f; do
    if [ "$f" != "{nginx_conf_dir}binhu.conf" ]; then
        echo "发现重复 80 端口配置: $f"
        mv "$f" "$f.bak" 2>/dev/null
    fi
done
echo "清理完成"
""", "处理端口冲突")

            # Also remove old site configs from 宝塔
            run(ssh, "rm -f /www/server/panel/vhost/nginx/*.conf 2>/dev/null; echo '宝塔旧配置已清理'")

        # Test nginx config
        out, err, code = run(ssh, "nginx -t 2>&1", "测试 Nginx 配置")
        if code != 0:
            print("⚠️ Nginx 配置测试失败，查看详情...")
            run(ssh, "cat /root/binhu/nginx/binhu.conf", "Nginx 配置内容")
        else:
            run(ssh, "nginx -s reload 2>&1 || systemctl reload nginx 2>&1 || service nginx reload 2>&1", "重载 Nginx")
            print("✅ Nginx 已重载")

        # ============================================
        # 步骤5: 停止旧容器，启动新的
        # ============================================
        print("\n【步骤5】启动 Docker Compose")

        # Clean up old containers
        run(ssh, """
cd /root/binhu
docker compose down --remove-orphans 2>/dev/null || true
docker rm -f binhu-mysql binhu-backend 2>/dev/null || true
echo "旧容器已清理"
""", "清理旧容器")

        # Build and start
        print("\n构建后端镜像...")
        out, err, code = run(ssh, "cd /root/binhu && docker compose build backend 2>&1", "构建 Backend 镜像", timeout=300)

        if code != 0:
            print("\n⚠️ Backend 构建出现问题，检查详情...")

        print("\n启动所有服务...")
        out, err, code = run(ssh, "cd /root/binhu && docker compose up -d 2>&1", "启动 docker compose", timeout=120)

        # ============================================
        # 步骤6: 等待并检查服务状态
        # ============================================
        print("\n【步骤6】等待服务就绪...")

        for i in range(24):  # Wait up to 2 minutes
            time.sleep(5)
            out, _, _ = run(ssh, "docker compose ps --format 'table {{.Name}}\t{{.Status}}' 2>&1", f"状态检查 ({i+1}/24)")

            # Check MySQL health
            out2, _, _ = run(ssh,
                "docker inspect binhu-mysql --format='{{.State.Health.Status}}' 2>/dev/null",
                ""
            )
            if "healthy" in out2:
                print("✅ MySQL 已就绪！")
                break
            elif "unhealthy" in out2:
                print("⚠️ MySQL 不健康，查看日志...")
                run(ssh, "docker logs binhu-mysql --tail 20 2>&1", "MySQL 日志")
                break
        else:
            print("⚠️ MySQL 启动超时")
            run(ssh, "docker logs binhu-mysql --tail 30 2>&1", "MySQL 日志")

        # Check Backend
        time.sleep(5)
        run(ssh, "docker logs binhu-backend --tail 30 2>&1", "Backend 日志")

        # ============================================
        # 步骤7: 验证部署
        # ============================================
        print("\n【步骤7】验证部署")

        run(ssh, "curl -s http://localhost:8000/api/health 2>&1", "后端健康检查 (直接)")
        run(ssh, "curl -s http://localhost/api/health 2>&1", "后端健康检查 (通过 Nginx)")
        run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://localhost/ 2>&1", "前端页面 (HTTP 状态码)")

        print("\n" + "=" * 60)
        print("  ✅ 部署流程完成！")
        print(f"  访问地址: http://{HOST}")
        print("=" * 60)

        ssh.close()

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
