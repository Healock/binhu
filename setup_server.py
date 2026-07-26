"""远程服务器配置脚本 - 安装 Nginx + 配置站点 + 启动 Docker"""
import paramiko
import os
import time
import sys

HOST = os.getenv("BINHU_SSH_HOST")
PORT = int(os.getenv("BINHU_SSH_PORT", "22"))
USER = os.getenv("BINHU_SSH_USER", "root")
PASSWORD = os.getenv("BINHU_SSH_PASSWORD")


def run_cmd(ssh, cmd, desc=""):
    """执行远程命令并打印输出"""
    if desc:
        print(f"\n{'='*50}")
        print(f"  {desc}")
        print(f"{'='*50}")

    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=300)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()

    if out:
        print(out)
    if err:
        print(f"[STDERR] {err}")

    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        print(f"  ⚠️  命令退出码: {exit_code}")
    return out, err, exit_code


def main():
    if not HOST or not PASSWORD:
        raise RuntimeError("Set BINHU_SSH_HOST and BINHU_SSH_PASSWORD before configuring the server.")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        print("连接服务器...")
        ssh.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=15)
        print("SSH 连接成功！")

        # 1. 检查 Docker
        run_cmd(ssh, "docker --version && docker-compose --version || docker compose version", "检查 Docker 环境")

        # 2. 安装 Nginx
        run_cmd(ssh, "which nginx || (apt-get update -qq && apt-get install -y nginx)", "安装 Nginx")

        # 3. 配置 Nginx
        run_cmd(ssh, "cp /root/binhu/nginx/binhu.conf /etc/nginx/conf.d/binhu.conf", "部署 Nginx 站点配置")
        run_cmd(ssh, "rm -f /etc/nginx/sites-enabled/default 2>/dev/null; rm -f /etc/nginx/conf.d/default.conf 2>/dev/null; echo 'ok'", "移除默认站点")

        # 4. 测试并重载 Nginx
        out, err, code = run_cmd(ssh, "nginx -t 2>&1 && systemctl restart nginx && echo 'Nginx 重启成功'", "测试并重启 Nginx")

        if code != 0:
            print("\n❌ Nginx 配置有误，停止部署")
            ssh.close()
            sys.exit(1)

        # 5. 启动 Docker Compose
        print("\n" + "="*50)
        print("  启动 Docker Compose (MySQL + Backend)")
        print("="*50)
        # 先拉取镜像
        run_cmd(ssh, "cd /root/binhu && docker compose pull 2>&1 || true", "拉取 Docker 镜像")
        # 构建并启动
        run_cmd(ssh, "cd /root/binhu && docker compose up -d --build 2>&1", "构建并启动容器")

        # 6. 等待容器启动
        print("\n等待服务启动...")
        time.sleep(10)

        # 7. 检查容器状态
        run_cmd(ssh, "cd /root/binhu && docker compose ps", "容器运行状态")

        # 8. 等待 MySQL 健康检查 + 查看 Backend 日志
        print("\n等待 MySQL 就绪 (最多60秒)...")
        for i in range(12):
            time.sleep(5)
            out, _, _ = run_cmd(ssh,
                "docker inspect binhu-mysql --format='{{.State.Health.Status}}' 2>/dev/null",
                f"检查 MySQL 健康状态 ({i+1}/12)"
            )
            status = out.strip()
            if status == "healthy":
                print("  ✅ MySQL 已就绪！")
                break
            else:
                print(f"  当前状态: {status}, 继续等待...")

        # 查看 Backend 日志
        run_cmd(ssh, "docker logs binhu-backend --tail 30 2>&1", "Backend 最近日志")

        ssh.close()
        print("\n✅ 服务器配置完成！")

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
