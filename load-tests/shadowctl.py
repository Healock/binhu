"""Safe operator entry point for shadow seed/run/verify/cleanup."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from fixture import write_manifest
from shadow_guard import ShadowSafetyError, require_shadow_context


ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="滨湖智慧平台影子压测安全工具")
    parser.add_argument("command", choices=("seed", "run", "verify", "cleanup"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--users", type=int, default=50, choices=(5, 50, 75))
    parser.add_argument("--duration", default="30m")
    return parser


def _manifest(run_id: str) -> Path:
    return ARTIFACTS / f"shadow-fixture-{run_id}.json"


def seed(run_id: str) -> int:
    context = require_shadow_context(run_id)
    path = write_manifest(ARTIFACTS, context.run_id)
    print(json.dumps({"run_id": context.run_id, "manifest": str(path), "fictional_only": True}, ensure_ascii=False))
    print("请由影子环境专用 seeder 读取该清单写入影子数据库；本命令不会连接正式数据库。")
    return 0


def run(run_id: str, users: int, duration: str) -> int:
    context = require_shadow_context(run_id)
    manifest = _manifest(context.run_id)
    if not manifest.is_file():
        raise ShadowSafetyError("找不到 seed 生成的影子数据清单")
    base_url = os.environ.get("SHADOW_BASE_URL", "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"/shadow-api", "/shadow-api/"}:
        raise ShadowSafetyError("SHADOW_BASE_URL 必须是 HTTPS 的固定 /shadow-api 地址")
    child_env = os.environ.copy()
    if users == 75:
        child_env["LOAD_TEST_BURST"] = "1"
    locust = [
        "locust", "-f", str(ROOT / "locustfile.py"),
        "--headless", "--users", str(users), "--spawn-rate", str(users),
        "--run-time", duration, "--host", base_url,
        "--csv", str(ARTIFACTS / f"{context.run_id}-locust"),
        "--html", str(ARTIFACTS / f"{context.run_id}-locust.html"),
    ]
    return subprocess.call(locust, cwd=ROOT, env=child_env)


def verify(run_id: str) -> int:
    context = require_shadow_context(run_id)
    path = _manifest(context.run_id)
    if not path.is_file():
        raise ShadowSafetyError("找不到影子清单")
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = data.get("tasks") or []
    users = data.get("users") or []
    expected = {"users": 76, "tasks": 3600, "communities": 12, "properties": 48}
    actual = {
        "users": len(users),
        "tasks": len(tasks),
        "communities": len(data.get("communities") or []),
        "properties": sum(len(item.get("properties") or []) for item in data.get("communities") or []),
    }
    if actual != expected:
        raise ShadowSafetyError(f"影子清单数量不符合预期：{actual} != {expected}")
    database_checks = _verify_database(context)
    print(json.dumps({"run_id": context.run_id, "manifest_counts": actual, "database_checks": database_checks}, ensure_ascii=False))
    return 0


def _verify_database(context) -> dict[str, object]:
    password = os.environ.get("SHADOW_DB_PASSWORD", "")
    if not password:
        raise ShadowSafetyError("verify 必须设置 SHADOW_DB_PASSWORD，不能只核对本地清单")
    try:
        import pymysql
    except ImportError as exc:
        raise ShadowSafetyError("verify 需要安装 load-tests/requirements.txt 中的 PyMySQL") from exc
    host = context.db_host
    port = int(os.environ.get("SHADOW_DB_PORT", "3306"))
    user = os.environ.get("SHADOW_DB_USER", "")
    if not user:
        raise ShadowSafetyError("verify 缺少 SHADOW_DB_USER")
    connection = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=context.db_name,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM _shadow_loadtest_marker WHERE run_id=%s AND environment='shadow'",
                (context.run_id,),
            )
            marker_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM _users WHERE username='observer@shadow' OR username LIKE 'loadtest-%' OR username LIKE 'burst-%'",
            )
            user_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count, MIN(revision) AS min_revision FROM _online_source_projection WHERE row_key LIKE 'shadow-%'",
            )
            task_row = cursor.fetchone()
    finally:
        connection.close()
    checks = {
        "marker": marker_count == 1,
        "users": user_count == 76,
        "tasks": int(task_row["count"] or 0) == 3600,
        "revision_initialized": int(task_row["min_revision"] or 0) >= 1,
    }
    if not all(checks.values()):
        raise ShadowSafetyError(f"影子数据库校验失败：{checks}")
    return {"checks": checks, "user_count": user_count, "task_count": int(task_row["count"] or 0)}


def cleanup(run_id: str) -> int:
    context = require_shadow_context(run_id)
    compose_file = ROOT / "docker-compose.shadow.yml"
    if not compose_file.is_file():
        raise ShadowSafetyError("找不到影子 Compose 文件")
    compose = shutil.which("docker")
    if not compose:
        raise ShadowSafetyError("未找到 Docker，不能执行影子资源清理")
    result = subprocess.run(
        [compose, "compose", "-p", context.project, "-f", str(compose_file), "down", "--volumes", "--remove-orphans"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode:
        return result.returncode
    prefix = f"{context.run_id}-"
    manifest = _manifest(context.run_id)
    if manifest.is_file():
        manifest.unlink()
    for path in ARTIFACTS.glob(f"{prefix}*"):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    print(json.dumps({"run_id": context.run_id, "cleanup": "artifacts_removed", "docker_project": context.project}, ensure_ascii=False))
    print("数据库和容器资源必须由同一 run_id 的影子编排清理，不允许清理其他项目。")
    return 0


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "seed": return seed(args.run_id)
        if args.command == "run": return run(args.run_id, args.users, args.duration)
        if args.command == "verify": return verify(args.run_id)
        return cleanup(args.run_id)
    except ShadowSafetyError as exc:
        print(f"安全检查失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
