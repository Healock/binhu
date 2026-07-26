"""修复脚本 - 配置 Docker 国内镜像 + 修正 Nginx 路径"""
import paramiko
import os
import time
import sys

HOST = os.getenv("BINHU_SSH_HOST")
PORT = int(os.getenv("BINHU_SSH_PORT", "22"))
USER = os.getenv("BINHU_SSH_USER", "root")
PASSWORD = os.getenv("BINHU_SSH_PASSWORD")


def run_cmd(ssh, cmd, desc=""):
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
    return out, err, exit_code


def main():
    if not HOST or not PASSWORD:
        raise RuntimeError("Set BINHU_SSH_HOST and BINHU_SSH_PASSWORD before running this script.")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=15)
        print("SSH 连接成功！")

        # 1. 检查 Nginx 实际配置路径
        run_cmd(ssh, "nginx -t 2>&1 | head -5", "检查 Nginx 配置路径")
        run_cmd(ssh, "ls /www/server/nginx/conf/ 2>/dev/null; ls /www/server/panel/vhost/nginx/ 2>/dev/null", "查找 Nginx 配置目录")

        # 2. 检查宝塔面板 Nginx 站点目录
        out, _, _ = run_cmd(ssh, "find /www/server -name '*.conf' -path '*/nginx/*' 2>/dev/null | head -20", "列出 Nginx 配置文件")

        # 3. 配置 Docker 国内镜像
        run_cmd(ssh, """
mkdir -p /etc/docker
cat > /etc/docker/daemon.json << 'EOF'
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.xuanyuan.me"
  ]
}
EOF
systemctl restart docker
echo "Docker 镜像加速器配置完成"
""", "配置 Docker 国内镜像源并重启")

        # 4. 验证 Docker pull
        run_cmd(ssh, "docker pull mysql:8.0 2>&1 | tail -5", "测试拉取 MySQL 镜像")

        # 5. 查找正确的 Nginx 配置位置
        out, _, _ = run_cmd(ssh, "cat /www/server/nginx/conf/nginx.conf 2>/dev/null | grep -E 'include|conf.d|vhost' | head -10", "查看 Nginx 主配置中的 include 指令")

        ssh.close()

    except Exception as e:
        print(f"❌ 错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
