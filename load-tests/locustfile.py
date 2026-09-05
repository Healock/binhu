"""Shadow flow-task workload using the same contracts as the Windows client."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import random
import threading
import time
from pathlib import Path
from urllib.parse import quote

from locust import HttpUser, task


PARSERS = ("全链条", "出租房屋核查", "寄递业", "疑似返苏", "苏州涉警", "交通涉警")
RESULT_FIELDS = {parser: ("核查反馈" if parser == "疑似返苏" else "核查结果") for parser in PARSERS}
RESULT_CHOICES = {
    "全链条": ("离苏", "无需登记"),
    "出租房屋核查": ("离苏", "常口"),
    "寄递业": ("离苏", "身份错误"),
    "疑似返苏": ("离苏", "无需登记"),
    "苏州涉警": ("离苏", "常口"),
    "交通涉警": ("离苏", "常口"),
}
API_PREFIX = "/shadow-api"
_account_counter = itertools.count()
_account_lock = threading.Lock()
_event_lock = threading.Lock()
_barrier_lock = threading.Lock()
_conflict_barriers: dict[int, threading.Barrier] = {}


def _is_conflict_scenario() -> bool:
    return os.environ.get("LOAD_TEST_SCENARIO") == "conflict"


def _barrier_for_pair(pair_index: int) -> threading.Barrier:
    """Return a usable two-party barrier for one coordinated conflict pair."""
    with _barrier_lock:
        barrier = _conflict_barriers.get(pair_index)
        if barrier is None or barrier.broken:
            barrier = threading.Barrier(2)
            _conflict_barriers[pair_index] = barrier
        return barrier


def _conflict_tasks(user_type: type) -> list:
    return [user_type.concurrent_conflict]


def _password(username: str) -> str:
    return f"LoadTest-{hashlib.sha256(username.encode()).hexdigest()[:16]}!"


def _core_accounts() -> list[str]:
    # The first five users form the smoke cohort.  The fixture deliberately
    # assigns pending-registration rows to members 31-35, so put them first to
    # make the five-user gate exercise the atomic registration path as well as
    # ordinary reads and saves.  The 50-user run still uses every core account
    # exactly once.
    names = [f"loadtest-member-{index:02d}" for index in range(31, 36)]
    names.extend(f"loadtest-member-{index:02d}" for index in range(1, 31))
    for role, count in (("leader", 8), ("internal_business", 4), ("admin", 2), ("super_admin", 1)):
        names.extend(f"loadtest-{role}-{index:02d}" for index in range(1, count + 1))
    return names


def _accounts() -> list[str]:
    if os.environ.get("LOAD_TEST_SCENARIO") == "conflict":
        paired = [name for index in range(1, 9)
                  for name in (f"loadtest-member-{index:02d}", f"loadtest-leader-{index:02d}")]
        return [*paired, "loadtest-internal_business-01", "loadtest-internal_business-02",
                "loadtest-admin-01", "loadtest-admin-02"]
    names = _core_accounts()
    if os.environ.get("LOAD_TEST_BURST") == "1":
        names.extend(f"burst-{index:02d}" for index in range(1, 26))
    return names


def _display_name(username: str) -> str:
    if username.startswith("burst-"):
        return f"压测突发组员{int(username.rsplit('-', 1)[1]):02d}"
    role, number = username.removeprefix("loadtest-").rsplit("-", 1)
    label = {"member": "组员", "leader": "组长", "internal_business": "基础管控",
             "admin": "管理员", "super_admin": "超级管理员"}[role]
    return f"压测{label}{int(number):02d}"


def _community(username: str) -> str:
    number = int(username.rsplit("-", 1)[1])
    return f"压测社区{(number - 1) % 12 + 1:02d}"


def _runtime_rows() -> list[dict]:
    path = os.environ.get("SHADOW_RUNTIME_INDEX", "").strip()
    if not path:
        return []
    return json.loads(Path(path).read_text(encoding="utf-8")).get("tasks", [])


RUNTIME_ROWS = _runtime_rows()


def _record(event: dict) -> None:
    path = os.environ.get("SHADOW_EVENT_LOG", "").strip()
    if not path:
        return
    event = {"at": time.time(), **event}
    with _event_lock:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def _api(path: str) -> str:
    return f"{API_PREFIX}{path}"


class FlowUser(HttpUser):
    abstract = False

    def wait_time(self) -> float:
        return random.uniform(0.5, 2) if os.environ.get("LOAD_TEST_BURST") == "1" else random.uniform(2, 6)

    def on_start(self) -> None:
        with _account_lock:
            index = next(_account_counter)
        accounts = _accounts()
        self.account_index = index
        self.username = accounts[index % len(accounts)]
        self.display_name = _display_name(self.username)
        self.community = _community(self.username)
        self.device_id = f"locust-{index:04d}"
        with self.client.post(
            _api("/auth/login"),
            json={"username": self.username, "password": _password(self.username),
                  "device_type": "windows", "device_id": self.device_id},
            name="POST /shadow-api/auth/login", catch_response=True,
        ) as response:
            self.ready = response.status_code < 400
            if not self.ready:
                response.failure(f"login {response.status_code}")
        self.parser_type = random.choice(PARSERS)
        self.task_row: dict | None = None
        self.live_rows: list[dict] = []
        self.invalid_targets: set[tuple[str, str]] = set()
        self.write_index = 0
        self.registration_index = 0
        # The conflict gate measures one precise contract: two clients read
        # the same revision and then submit together.  Do not mix the normal
        # weighted browsing workload into this scenario.  A low-weight
        # conflict task can otherwise miss its partner, break its barrier and
        # finish a nominal conflict run without producing a single round.
        if _is_conflict_scenario():
            self.tasks = _conflict_tasks(type(self))

    def _scope(self) -> str:
        if "-member-" in self.username or self.username.startswith("burst-"):
            return "mine"
        return "community" if "-leader-" in self.username else "all"

    def _eligible_rows(self, scenario: str) -> list[dict]:
        rows = [
            row for row in RUNTIME_ROWS
            if row.get("scenario") == scenario
            and (str(row.get("parser_type") or ""), str(row.get("row_key") or ""))
            not in self.invalid_targets
        ]
        if "-member-" in self.username or self.username.startswith("burst-"):
            if scenario == "unassigned":
                return [row for row in rows if row.get("community") == self.community]
            return [row for row in rows if row.get("inspector") == self.display_name]
        if "-leader-" in self.username:
            return [row for row in rows if row.get("community") == self.community]
        return rows

    def _list(self) -> dict:
        if not self.ready:
            return {}
        response = self.client.get(
            _api(f"/mobile-tasks/{quote(self.parser_type, safe='')}"),
            params={"scope": self._scope(), "status": "all", "page": 1,
                    "page_size": 20, "sort": "priority"},
            name="GET /shadow-api/mobile-tasks/{parser_type}",
        )
        if response.ok:
            payload = response.json()
            rows = payload.get("data") or payload.get("items") or []
            self.live_rows = list(rows)
            if rows:
                self.task_row = rows[0]
            return payload
        return {}

    def _source(self, target: dict | None = None) -> tuple[str, str, dict] | None:
        if not self.ready:
            return None
        parser_type = str((target or {}).get("parser_type") or self.parser_type)
        row_key = str((target or {}).get("row_key") or (self.task_row or {}).get("row_key") or "")
        if not row_key:
            self._list()
            row_key = str((self.task_row or {}).get("row_key") or "")
        if not row_key:
            return None
        with self.client.get(
            _api(f"/mobile-tasks/{quote(parser_type, safe='')}/{quote(row_key, safe='')}"),
            name="GET /shadow-api/mobile-tasks/{parser_type}/{row_key}",
            catch_response=True,
        ) as response:
            if not response.ok:
                self.invalid_targets.add((parser_type, row_key))
                self.task_row = None
                response.failure(f"detail {response.status_code}")
                return None
            payload = response.json()
        sources = payload.get("sources") or payload.get("task", {}).get("sources") or []
        return (parser_type, row_key, sources[0]) if sources else None

    @task(20)
    def browse_and_filter(self) -> None:
        self._list()
        if random.random() < 0.4:
            self.client.get(
                _api(f"/mobile-tasks/{quote(self.parser_type, safe='')}"),
                params={"scope": self._scope(), "status": "all", "keyword": "压测",
                        "page": 1, "page_size": 20},
                name="GET /shadow-api/mobile-tasks/{parser_type} filtered",
            )

    @task(10)
    def open_detail(self) -> None:
        self._source()

    def _save(self, changes: dict[str, str], *, scenario: str = "assigned", claim: bool = False) -> None:
        if scenario == "assigned":
            self._list()
            eligible = [
                {"parser_type": self.parser_type, "row_key": row.get("row_key")}
                for row in self.live_rows
                if row.get("row_key")
                and (self.parser_type, str(row.get("row_key"))) not in self.invalid_targets
            ]
        else:
            eligible = self._eligible_rows(scenario)
        # Never fall back to the user's last-opened ordinary task when a
        # scenario has no eligible fixture.  That would turn, for example, a
        # pending-registration request into an invalid save without a selected
        # property and pollute the load-test failure rate with harness errors.
        if not eligible:
            return
        target = random.choice(eligible)
        source_info = self._source(target)
        if not source_info:
            return
        parser_type, row_key, source = source_info
        changes = {
            (RESULT_FIELDS[parser_type] if field == "__result__" else field): value
            for field, value in changes.items()
        }
        # A user may pick the same task more than once during a run.  The
        # API intentionally rejects a no-op save with 400; that is a harness
        # condition, not a platform failure.  Drop unchanged fields before
        # constructing the request so the workload measures real writes.
        current_values = source.get("values") or {}
        changes = {
            field: value
            for field, value in changes.items()
            if str(current_values.get(field) or "").strip() != str(value or "").strip()
        }
        if not changes:
            return
        source_id = str(source.get("id") or source.get("source_id") or "")
        revision = int(source.get("revision") or 1)
        body: dict[str, object] = {"changes": changes, "base_values": source.get("values") or {},
                                  "expected_revision": revision}
        selected_property: dict[str, int] | None = None
        if scenario == "pending_registration" and target:
            candidates = target.get("property_candidates") or []
            if candidates:
                self.registration_index += 1
                selected_property = candidates[self.registration_index % len(candidates)]
            else:
                selected_property = {
                    "property_id": int(target["property_id"]),
                    "property_version": int(target["property_version"]),
                }
            body["registration_property_id"] = int(selected_property["property_id"])
            body["registration_property_version"] = int(selected_property["property_version"])
        path = _api(f"/mobile-tasks/{quote(parser_type, safe='')}/source-rows/{source_id}")
        method = self.client.post if claim else self.client.patch
        if claim:
            path += "/claim"
        with method(
            path, json=body,
            name=("POST claim and save" if claim else "PATCH mobile task save"),
            catch_response=True,
        ) as response:
            payload = (
                response.json()
                if response.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            _record({
                "kind": "claim" if claim else "write",
                "scenario": scenario, "username": self.username,
                "inspector": self.display_name if claim else "",
                "parser_type": parser_type, "row_key": row_key,
                "source_id": int(source_id), "read_revision": revision,
                "status": response.status_code,
                "returned_revision": payload.get("revision"),
                "operation_id": payload.get("operation_id"),
                "derived_status": payload.get("derived_status"),
                "error_code": (payload.get("detail") or {}).get("code")
                    if isinstance(payload.get("detail"), dict) else "",
                "failed_operation_id": (payload.get("detail") or {}).get("operation_id")
                    if isinstance(payload.get("detail"), dict) else "",
                "property_id": int(selected_property["property_id"]) if selected_property else None,
                "property_version": int(selected_property["property_version"]) if selected_property else None,
                "changes_sha256": hashlib.sha256(
                    json.dumps(changes, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
                "changes": changes,
            })
            if response.status_code == 409:
                response.success()
                return
            if response.status_code >= 400:
                response.failure(f"save {response.status_code}")

    @task(35)
    def autosave_text(self) -> None:
        self.write_index += 1
        self._save({"现住址": f"压测自动保存地址-{self.device_id}-{self.write_index}"})

    @task(20)
    def save_result(self) -> None:
        self.write_index += 1
        choices = RESULT_CHOICES[self.parser_type]
        self._save({"__result__": choices[self.write_index % len(choices)]})

    @task(5)
    def claim_and_save(self) -> None:
        if "-member-" in self.username or self.username.startswith("burst-"):
            self.write_index += 1
            self._save(
                {"现住址": f"压测领取地址-{self.device_id}-{self.write_index}"},
                scenario="unassigned", claim=True,
            )

    @task(5)
    def registration_atomic_save(self) -> None:
        self._save({"__result__": "待登记"},
                   scenario="pending_registration")

    @task(5)
    def concurrent_conflict(self) -> None:
        if not _is_conflict_scenario():
            return
        targets = [row for row in RUNTIME_ROWS if row.get("scenario") == "conflict"][:10]
        if not targets:
            return
        pair_index = (self.account_index // 2) % len(targets)
        source_info = self._source(targets[pair_index])
        if not source_info:
            return
        parser_type, row_key, source = source_info
        source_id = int(source.get("id") or source.get("source_id") or 0)
        revision = int(source.get("revision") or 1)
        barrier = _barrier_for_pair(pair_index)
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            return
        # All six seeded parser schemas expose 现住址 as an editable field.
        # 备注 is not part of every parser contract and made an otherwise
        # correctly coordinated request fail with 400 before revision locking.
        changes = {"现住址": f"压测冲突地址-{self.device_id}-{time.time_ns()}"}
        with self.client.patch(
            _api(f"/mobile-tasks/{quote(parser_type, safe='')}/source-rows/{source_id}"),
            json={"changes": changes, "base_values": source.get("values") or {},
                  "expected_revision": revision}, name="PATCH coordinated conflict",
            catch_response=True,
        ) as response:
            payload = (
                response.json()
                if response.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            _record({
                "kind": "conflict", "pair": pair_index, "username": self.username,
                "parser_type": parser_type, "row_key": row_key, "source_id": source_id,
                "read_revision": revision, "status": response.status_code,
                "returned_revision": payload.get("revision"),
                "operation_id": payload.get("operation_id"),
                "error_code": (payload.get("detail") or {}).get("code")
                    if isinstance(payload.get("detail"), dict) else "",
                "failed_operation_id": (payload.get("detail") or {}).get("operation_id")
                    if isinstance(payload.get("detail"), dict) else "",
                "changes": changes,
            })
            if response.status_code == 409:
                response.success()
            elif response.status_code >= 400:
                response.failure(f"conflict {response.status_code}")
