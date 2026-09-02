"""Fail-closed checks shared by every shadow load-test command.

This module deliberately does not provide a force/unsafe switch.  A load test
must prove that it targets a separately labelled environment before it can
create data, send traffic, inspect results, or delete resources.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


RUN_ID_RE = re.compile(r"^LT-[0-9]{8}-[0-9]{2}$")
PROJECT_PREFIX = "binhu-loadtest-"
SHADOW_DB_HOST = "127.0.0.1"
SHADOW_DB_PORT = 47126


class ShadowSafetyError(RuntimeError):
    """Raised when a command cannot prove it is isolated from production."""


@dataclass(frozen=True)
class ShadowContext:
    run_id: str
    project: str
    db_host: str
    db_port: int
    db_name: str


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
    try:
        db_port = int(_required(env, "SHADOW_DB_PORT"))
    except ValueError as exc:
        raise ShadowSafetyError("SHADOW_DB_PORT 必须是整数") from exc
    if db_host != SHADOW_DB_HOST or db_port != SHADOW_DB_PORT:
        raise ShadowSafetyError(
            f"影子数据库只允许连接 {SHADOW_DB_HOST}:{SHADOW_DB_PORT}"
        )
    db_name = _required(env, "SHADOW_DB_NAME")
    if not re.fullmatch(r"LoadTest_[A-Za-z0-9_]+", db_name):
        raise ShadowSafetyError("影子数据库名称必须以 LoadTest_ 开头且只含字母、数字和下划线")
    if db_name.lower() in {"onlinedata", "registrydata", "daily_report", "platformdata"}:
        raise ShadowSafetyError("数据库名称属于正式业务库，已拒绝")
    return ShadowContext(normalized_run_id, project, db_host, db_port, db_name)


def require_shadow_context(run_id: str) -> ShadowContext:
    return validate_shadow_environment(run_id)
