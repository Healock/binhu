"""真实客户端相同 API 契约的影子流口任务负载场景。

仅向 SHADOW_BASE_URL 发请求。409 乐观锁冲突属于预期并发结果，不会用旧
revision 重试；其他非 2xx 响应会被 Locust 计为失败。
"""

from __future__ import annotations

import itertools
import os
import random
import threading
from urllib.parse import quote

from locust import HttpUser, task


PARSERS = ("全链条", "出租房屋核查", "寄递业", "疑似返苏", "苏州涉警", "交通涉警")
PASSWORD_PREFIX = "LoadTest-"
_account_counter = itertools.count()
_account_lock = threading.Lock()


def _password(username: str) -> str:
    import hashlib
    return f"{PASSWORD_PREFIX}{hashlib.sha256(username.encode()).hexdigest()[:16]}!"


def _accounts() -> list[str]:
    names = ["observer@shadow"]
    for role, count in (("member", 35), ("leader", 8), ("internal_business", 4), ("admin", 2), ("super_admin", 1)):
        names.extend(f"loadtest-{role}-{index:02d}" for index in range(1, count + 1))
    names.extend(f"burst-{index:02d}" for index in range(1, 26))
    return names


class FlowUser(HttpUser):
    abstract = False

    def wait_time(self) -> float:
        return random.uniform(0.5, 2) if os.environ.get("LOAD_TEST_BURST") == "1" else random.uniform(2, 6)

    def on_start(self) -> None:
        with _account_lock:
            index = next(_account_counter)
        accounts = _accounts()
        self.username = accounts[index % len(accounts)]
        self.device_id = f"locust-{index:04d}"
        response = self.client.post(
            "/api/auth/login",
            json={
                "username": self.username,
                "password": _password(self.username),
                "device_type": "windows",
                "device_id": self.device_id,
            },
            name="POST /api/auth/login",
        )
        if response.status_code >= 400:
            response.failure(f"login {response.status_code}")
            self.ready = False
            return
        self.ready = True
        self.parser_type = random.choice(PARSERS)
        self.task_row: dict | None = None

    def _list(self) -> dict:
        if not getattr(self, "ready", False):
            return {}
        response = self.client.get(
            f"/api/mobile-tasks/{quote(self.parser_type, safe='')}",
            params={"scope": "all", "status": "all", "page": 1, "page_size": 20, "sort": "priority"},
            name="GET /api/mobile-tasks/{parser_type}",
        )
        if response.ok:
            payload = response.json()
            rows = payload.get("data") or payload.get("items") or []
            if rows:
                self.task_row = rows[0]
            return payload
        return {}

    @task(20)
    def browse_and_filter(self) -> None:
        self._list()
        if random.random() < 0.4:
            self.client.get(
                f"/api/mobile-tasks/{quote(self.parser_type, safe='')}",
                params={"scope": "all", "status": "all", "keyword": "压测", "page": 1, "page_size": 20},
                name="GET /api/mobile-tasks/{parser_type} filtered",
            )

    def _detail_source(self) -> tuple[str, dict] | None:
        if not getattr(self, "ready", False):
            return None
        if not self.task_row:
            self._list()
        if not self.task_row:
            return None
        row_key = str(self.task_row.get("row_key") or "")
        if not row_key:
            return None
        response = self.client.get(
            f"/api/mobile-tasks/{quote(self.parser_type, safe='')}/{quote(row_key, safe='')}",
            name="GET /api/mobile-tasks/{parser_type}/{row_key}",
        )
        if not response.ok:
            return None
        payload = response.json()
        sources = payload.get("sources") or payload.get("task", {}).get("sources") or []
        if not sources:
            return None
        source = sources[0]
        return str(source.get("id") or source.get("source_id") or ""), source

    @task(10)
    def open_detail(self) -> None:
        self._detail_source()

    def _save(self, changes: dict[str, str], *, claim: bool = False) -> None:
        source_info = self._detail_source()
        if not source_info:
            return
        source_id, source = source_info
        revision = int(source.get("revision") or 1)
        body = {"changes": changes, "base_values": source.get("values") or {}, "expected_revision": revision}
        path = f"/api/mobile-tasks/{quote(self.parser_type, safe='')}/source-rows/{source_id}"
        if claim:
            path += "/claim"
        response = self.client.patch(path, json=body, name="PATCH mobile task save")
        if response.status_code == 409:
            return
        if response.status_code >= 400:
            response.failure(f"save {response.status_code}")

    @task(35)
    def autosave_text(self) -> None:
        self._save({"备注": f"压测备注-{self.device_id}"})

    @task(20)
    def save_result(self) -> None:
        self._save({"核查结果": "已核查"})

    @task(5)
    def claim_and_save(self) -> None:
        self._save({"备注": f"压测领取-{self.device_id}"}, claim=True)

    @task(5)
    def registration_atomic_save(self) -> None:
        self._save({"核查结果": "待登记", "备注": f"压测登记-{self.device_id}"})

    @task(5)
    def concurrent_conflict(self) -> None:
        # Two users independently read the same revision; the server must
        # accept at most one and return one 409 without stale-value retry.
        self._save({"备注": f"压测冲突-{self.device_id}"})
