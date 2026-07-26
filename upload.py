"""SFTP 批量上传脚本 - 将项目文件传输到服务器"""
import paramiko
import os
import sys

HOST = os.getenv("BINHU_SSH_HOST")
PORT = int(os.getenv("BINHU_SSH_PORT", "22"))
USER = os.getenv("BINHU_SSH_USER", "root")
PASSWORD = os.getenv("BINHU_SSH_PASSWORD")
REMOTE_BASE = "/root/binhu"
LOCAL_BASE = os.getenv("BINHU_LOCAL_BASE", os.path.dirname(os.path.abspath(__file__)))

# 需要排除的文件/文件夹
EXCLUDE = {
    "__pycache__",
    ".pyc",
    "node_modules",
    ".git",
    ".workbuddy",
    "upload.py",
    ".env",
    "frontend/src",
    "frontend/public",
    "frontend/node_modules",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/vite.config.ts",
    "frontend/tsconfig.json",
    "frontend/tsconfig.node.json",
    "frontend/postcss.config.cjs",
    "frontend/index.html",
    "frontend/.gitignore",
}

# 需要上传的文件/目录列表 (相对于 LOCAL_BASE)
UPLOAD_ITEMS = [
    "docker-compose.yml",
    "backend",
    "frontend/dist",
    "nginx",
]


def create_remote_dir(sftp, remote_path):
    """递归创建远程目录"""
    try:
        sftp.stat(remote_path)
    except FileNotFoundError:
        parent = os.path.dirname(remote_path)
        if parent and parent != "/":
            create_remote_dir(sftp, parent)
        sftp.mkdir(remote_path)
        print(f"  [MKDIR] {remote_path}")


def upload_dir(sftp, local_dir, remote_dir):
    """递归上传目录"""
    for item in os.listdir(local_dir):
        if item in EXCLUDE:
            continue

        local_path = os.path.join(local_dir, item)
        remote_path = remote_dir + "/" + item

        if os.path.isdir(local_path):
            create_remote_dir(sftp, remote_path)
            upload_dir(sftp, local_path, remote_path)
        else:
            # 跳过 .pyc 文件
            if item.endswith(".pyc"):
                continue
            try:
                sftp.put(local_path, remote_path)
                print(f"  [UPLOAD] {remote_path}")
            except Exception as e:
                print(f"  [ERROR] {remote_path}: {e}")


def main():
    if not HOST or not PASSWORD:
        raise RuntimeError("Set BINHU_SSH_HOST and BINHU_SSH_PASSWORD before uploading.")
    print(f"连接服务器 {HOST}:{PORT} ...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=15)
        print("SSH 连接成功！")

        # 创建远程根目录
        sftp = ssh.open_sftp()
        create_remote_dir(sftp, REMOTE_BASE)

        # 上传每个项目
        for item in UPLOAD_ITEMS:
            local_path = os.path.join(LOCAL_BASE, item)
            remote_path = REMOTE_BASE + "/" + item

            if os.path.isdir(local_path):
                print(f"\n上传目录: {item}")
                create_remote_dir(sftp, remote_path)
                upload_dir(sftp, local_path, remote_path)
            else:
                print(f"\n上传文件: {item}")
                sftp.put(local_path, remote_path)
                print(f"  [UPLOAD] {remote_path}")

        sftp.close()

        # 验证上传结果
        print("\n\n验证上传结果...")
        stdin, stdout, stderr = ssh.exec_command(f"find {REMOTE_BASE} -type f | sort")
        files = stdout.read().decode().strip().split("\n")
        print(f"共上传 {len(files)} 个文件:")
        for f in files:
            print(f"  {f}")

        ssh.close()
        print("\n✅ 文件传输完成！")

    except Exception as e:
        print(f"❌ 错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
