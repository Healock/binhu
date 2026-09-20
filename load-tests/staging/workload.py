from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


def retry_delay(
    base_seconds: float,
    failures: int,
    *,
    maximum_seconds: float,
    jitter_ratio: float = .25,
    random_value: Callable[[], float] = random.random,
) -> float:
    exponent = max(0, int(failures) - 1)
    raw = min(float(maximum_seconds), float(base_seconds) * (2 ** exponent))
    jitter = raw * max(0.0, float(jitter_ratio))
    return max(.05, raw - jitter + (2 * jitter * random_value()))


@dataclass(frozen=True)
class RuntimeAccount:
    username: str
    role: str
    scope: str


@dataclass(frozen=True)
class RuntimeIndex:
    run_id: str
    accounts: tuple[RuntimeAccount, ...]
    tasks: tuple[dict[str, Any], ...]
    property_search_keyword: str


def load_runtime_index(path: str, expected_run_id: str) -> RuntimeIndex:
    target = Path(path)
    if not path or not target.is_file():
        raise RuntimeError("STAGING_RUNTIME_INDEX 必须指向受控 Staging 运行索引")
    payload = json.loads(target.read_text(encoding="utf-8"))
    if payload.get("run_id") != expected_run_id:
        raise RuntimeError("Staging 运行索引与当前运行编号不一致")
    if payload.get("environment") != "staging" or payload.get("fictional_only") is not True:
        raise RuntimeError("Staging 运行索引未声明隔离脱敏数据")
    if payload.get("production_data") is not False:
        raise RuntimeError("Staging 运行索引禁止包含生产数据")
    raw_accounts = payload.get("accounts") or []
    accounts: list[RuntimeAccount] = []
    for item in raw_accounts:
        username = str(item.get("username") or "").strip()
        role = str(item.get("role") or "member").strip()
        scope = str(item.get("scope") or ("mine" if role == "member" else "all")).strip()
        if not username.endswith("@staging"):
            raise RuntimeError("Staging 压测账号必须使用 @staging 后缀")
        accounts.append(RuntimeAccount(username=username, role=role, scope=scope))
    if len(accounts) < 75:
        raise RuntimeError("Staging 运行索引至少需要 75 个隔离测试账号")
    tasks = tuple(item for item in (payload.get("tasks") or []) if isinstance(item, dict))
    if not tasks:
        raise RuntimeError("Staging 运行索引缺少脱敏任务")
    for item in tasks:
        if not item.get("parser_type") or not item.get("row_key"):
            raise RuntimeError("Staging 运行索引任务缺少 parser_type 或 row_key")
    keyword = str(payload.get("property_search_keyword") or "").strip()
    if not keyword:
        raise RuntimeError("Staging 运行索引缺少脱敏房屋搜索词")
    return RuntimeIndex(expected_run_id, tuple(accounts), tasks, keyword)
