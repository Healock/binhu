from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse


class StagingSafetyError(RuntimeError):
    pass


RUN_ID_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")


@dataclass(frozen=True)
class StagingContext:
    run_id: str
    project: str
    base_url: str
    db_name: str


def validate_staging_environment(run_id: str, environ: dict[str, str] | None = None) -> StagingContext:
    env = dict(os.environ if environ is None else environ)
    normalized = run_id.strip().upper()
    if not RUN_ID_RE.fullmatch(normalized):
        raise StagingSafetyError("运行编号必须使用 STG-YYYYMMDD-NN 格式")
    if env.get("APP_ENVIRONMENT", "").strip().lower() != "staging":
        raise StagingSafetyError("APP_ENVIRONMENT 必须严格为 staging")
    if env.get("STAGING_LOAD_TEST_RUN_ID", "").strip().upper() != normalized:
        raise StagingSafetyError("运行编号与 STAGING_LOAD_TEST_RUN_ID 不一致")
    project = env.get("COMPOSE_PROJECT_NAME", "").strip()
    expected = f"binhu-staging-load-{normalized.lower()}"
    if project != expected:
        raise StagingSafetyError(f"Compose 项目必须严格为 {expected}")
    base_url = env.get("STAGING_BASE_URL", "").strip()
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise StagingSafetyError("STAGING_BASE_URL 必须是无凭据的 HTTPS origin")
    if not base_url:
        raise StagingSafetyError("缺少 STAGING_BASE_URL")
    db_name = env.get("STAGING_DB_NAME", "").strip()
    if not re.fullmatch(r"StagingLoad_[A-Za-z0-9_]+", db_name):
        raise StagingSafetyError("Staging 数据库必须使用 StagingLoad_ 前缀")
    forbidden = ("production", "onlinedata", "registrydata", "daily_report", "platformdata", "shadow")
    if any(item in db_name.lower() for item in forbidden):
        raise StagingSafetyError("数据库目标疑似正式或影子环境，已拒绝")
    for key in ("STAGING_LOAD_TEST_PASSWORD",):
        if not env.get(key, "").strip():
            raise StagingSafetyError(f"缺少 Staging 专用凭据 {key}")
    return StagingContext(normalized, project, base_url.rstrip("/"), db_name)


def require_staging_context(run_id: str) -> StagingContext:
    return validate_staging_environment(run_id)
