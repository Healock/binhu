"""Fail-closed checks shared by every shadow load-test command.

This module deliberately does not provide a force/unsafe switch.  A load test
must prove that it targets a separately labelled environment before it can
create data, send traffic, inspect results, or delete resources.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


RUN_ID_RE = re.compile(r"^LT-[0-9]{8}-[0-9]{2}$")
PROJECT_PREFIX = "binhu-loadtest-"
PRODUCTION_HOSTS = {
    "binhu-mysql",
    "binhu-backend",
    "production",
    "prod",
    "localhost",
    "127.0.0.1",
    "::1",
}


class ShadowSafetyError(RuntimeError):
    """Raised when a command cannot prove it is isolated from production."""


@dataclass(frozen=True)
class ShadowContext:
    run_id: str
    project: str
    db_host: str
    db_name: str
    marker_file: Path


def _required(env: dict[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ShadowSafetyError(f"缺少影子环境变量 {key}")
    return value


def validate_run_id(run_id: str) -> str:
    normalized = run_id.strip().upper()
    if not RUN_ID_RE.fullmatch(normalized):
        raise ShadowSafetyError("运行编号必须使用 LT-YYYYMMDD-NN 格式")
    return normalized


def validate_shadow_environment(run_id: str, environ: dict[str, str] | None = None) -> ShadowContext:
    env = dict(os.environ if environ is None else environ)
    normalized_run_id = validate_run_id(run_id)
    if env.get("APP_ENVIRONMENT", "").strip().lower() != "shadow":
        raise ShadowSafetyError("APP_ENVIRONMENT 必须严格为 shadow")
    configured_run = validate_run_id(_required(env, "LOAD_TEST_RUN_ID"))
    if configured_run != normalized_run_id:
        raise ShadowSafetyError("命令运行编号与 LOAD_TEST_RUN_ID 不一致")
    project = _required(env, "COMPOSE_PROJECT_NAME")
    expected_project = f"{PROJECT_PREFIX}{normalized_run_id.lower()}"
    if project != expected_project:
        raise ShadowSafetyError(f"Compose 项目必须严格为 {expected_project}")
    db_host = _required(env, "SHADOW_DB_HOST").lower()
    if db_host in PRODUCTION_HOSTS or db_host.startswith("binhu-"):
        raise ShadowSafetyError("数据库主机看起来是正式环境，已拒绝")
    db_name = _required(env, "SHADOW_DB_NAME")
    if db_name.lower() in {"onlinedata", "registrydata", "daily_report", "platformdata"}:
        raise ShadowSafetyError("数据库名称属于正式业务库，已拒绝")
    marker_file = Path(_required(env, "SHADOW_MARKER_FILE")).resolve()
    if not marker_file.is_file():
        raise ShadowSafetyError("影子标记文件不存在；请确认影子数据库已初始化")
    marker = marker_file.read_text(encoding="utf-8").strip()
    if marker != f"shadow:{normalized_run_id}":
        raise ShadowSafetyError("影子标记与运行编号不一致")
    return ShadowContext(normalized_run_id, project, db_host, db_name, marker_file)


def require_shadow_context(run_id: str) -> ShadowContext:
    return validate_shadow_environment(run_id)
