"""Realistic Staging workload for 75 concurrent users.

Core business actions remain Locust tasks.  Browser background work runs on
independent schedules so heartbeats, unread counts and reconnects do not line
up merely because a Locust task weight happened to fire.
"""
from __future__ import annotations

import itertools
import json
import os
import random
import threading
import time
from pathlib import Path
from urllib.parse import quote

import gevent
from gevent.event import Event
from locust import HttpUser, between, events, task

try:
    from .guard import require_staging_context
    from .metrics import GLOBAL_METRICS
    from .stream_clients import StreamHooks, run_query_websocket, run_sse
    from .workload import load_runtime_index, retry_delay
except ImportError:  # Locust imports this file as its working module
    from guard import require_staging_context
    from metrics import GLOBAL_METRICS
    from stream_clients import StreamHooks, run_query_websocket, run_sse
    from workload import load_runtime_index, retry_delay


RUN_ID = os.environ.get("STAGING_LOAD_TEST_RUN_ID", "")
CONTEXT = require_staging_context(RUN_ID)
RUNTIME = load_runtime_index(os.environ.get("STAGING_RUNTIME_INDEX", ""), CONTEXT.run_id)
PASSWORD = os.environ.get("STAGING_LOAD_TEST_PASSWORD", "")
if not PASSWORD:
    raise RuntimeError("缺少 STAGING_LOAD_TEST_PASSWORD")

API_PREFIX = "/staging/api"
PARSERS = tuple(sorted({str(item["parser_type"]) for item in RUNTIME.tasks}))
_ACCOUNT_COUNTER = itertools.count()
_ACCOUNT_LOCK = threading.Lock()


def _api(path: str) -> str:
    return f"{API_PREFIX}{path}"


def _encoded(value: str) -> str:
    return quote(str(value), safe="")


@events.request.add_listener
def _request_metric(
    request_type: str,
    name: str,
    response_time: float,
    response_length: int,
    exception=None,
    **kwargs,
) -> None:
    del request_type, response_length, kwargs
    if name and (name.startswith("core.") or name.startswith("poll.") or name.startswith("stream.")):
        GLOBAL_METRICS.observe(name, response_time, exception is not None)


@events.test_stop.add_listener
def _write_load_report(environment, **kwargs) -> None:
    del environment, kwargs
    target = os.environ.get("STAGING_LOAD_REPORT", "").strip()
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "run_id": CONTEXT.run_id,
        "environment": "staging",
        "production_data": False,
        **GLOBAL_METRICS.report(),
    }, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")


class StagingUser(HttpUser):
    host = CONTEXT.base_url
    wait_time = between(1.5, 5.0)

    def on_start(self) -> None:
        with _ACCOUNT_LOCK:
            index = next(_ACCOUNT_COUNTER)
        self.account_index = index
        self.account = RUNTIME.accounts[index % len(RUNTIME.accounts)]
        self.username = self.account.username
        self.parser = PARSERS[index % len(PARSERS)]
        self.device_id = f"{CONTEXT.run_id.lower()}-{index:03d}"
        self.ready = False
        self._stop = Event()
        self._greenlets: list[gevent.Greenlet] = []
        self._write_index = 0
        with self.client.post(_api("/auth/login"), json={
            "username": self.username,
            "password": PASSWORD,
            "device_type": "staging-load",
            "device_id": self.device_id,
        }, name="core.login", catch_response=True) as response:
            self.ready = response.status_code < 400
            if not self.ready:
                response.failure(f"login {response.status_code}")
        if not self.ready:
            return

        # Every authenticated browser keeps the global event stream.  Query
        # WebSocket is opened by one quarter of users, matching users who keep
        # the spreadsheet page open during normal work.
        self._spawn_stream("sse")
        if index % 4 == 0:
            self._spawn_stream("websocket")

        self._spawn_poll(30, self._heartbeat)
        self._spawn_poll(30, self._unread)
        self._spawn_poll(60, self._identity)
        self._spawn_poll(30, self._visible_task_refresh)
        self._spawn_poll(30, self._inline_editors)
        if index % 3 == 0:
            self._spawn_poll(45, self._property_search)
        if index % 4 == 0:
            self._spawn_poll(30, self._query_data)
            self._spawn_poll(15, self._query_version)
        # Maintenance polling belongs to login pages, not every authenticated
        # page.  A bounded subset represents idle login tabs without adding 75
        # artificial maintenance clients.
        if index % 5 == 0:
            self._spawn_poll(30, self._maintenance)

    def on_stop(self) -> None:
        self._stop.set()
        for worker in self._greenlets:
            worker.kill(block=False)
        self._greenlets.clear()

    def _scope(self) -> str:
        return self.account.scope if self.account.scope in {"mine", "community", "all"} else "mine"

    def _tasks(self, scenario: str | None = None, parser: str | None = None) -> list[dict]:
        result = []
        for item in RUNTIME.tasks:
            if scenario and str(item.get("scenario") or "assigned") != scenario:
                continue
            if parser and str(item.get("parser_type")) != parser:
                continue
            assigned_username = str(item.get("assigned_username") or "")
            if assigned_username and assigned_username != self.username:
                continue
            claim_username = str(item.get("claim_username") or "")
            if scenario == "unassigned" and claim_username and claim_username != self.username:
                continue
            assigner_username = str(item.get("assigner_username") or "")
            if scenario == "assignable" and assigner_username and assigner_username != self.username:
                continue
            result.append(item)
        return result

    def _spawn_poll(self, interval: float, request_once) -> None:
        self._greenlets.append(gevent.spawn(self._poll_loop, interval, request_once))

    def _poll_loop(self, interval: float, request_once) -> None:
        if self._stop.wait(timeout=random.uniform(0.0, interval)):
            return
        failures = 0
        while not self._stop.is_set():
            try:
                succeeded = bool(request_once())
            except Exception:
                succeeded = False
            if succeeded:
                failures = 0
                delay = interval
            else:
                failures += 1
                delay = 120 if failures >= 4 else retry_delay(interval, failures, maximum_seconds=120)
            if self._stop.wait(timeout=delay):
                return

    def _stream_hooks(self) -> StreamHooks:
        def opened(kind: str, reconnect: bool, latency_ms: float) -> None:
            GLOBAL_METRICS.stream_opened(reconnect=reconnect)
            events.request.fire(
                request_type=kind.upper(), name=f"stream.{kind}.connect",
                response_time=latency_ms, response_length=0, exception=None,
            )

        def closed(kind: str) -> None:
            del kind
            GLOBAL_METRICS.stream_closed()

        def reconnecting(kind: str, attempt: int, observed_at: float) -> None:
            del kind, attempt
            GLOBAL_METRICS.stream_reconnect_started(observed_at)

        def failed(kind: str, error_type: str) -> None:
            events.request.fire(
                request_type=kind.upper(), name=f"stream.{kind}.connect",
                response_time=0, response_length=0,
                exception=RuntimeError(error_type),
            )

        return StreamHooks(opened=opened, closed=closed, reconnecting=reconnecting, failed=failed)

    def _spawn_stream(self, kind: str) -> None:
        def start() -> None:
            if self._stop.wait(timeout=random.uniform(.25, 12.0)):
                return
            cookies = self.client.cookies.get_dict()
            common = {
                "base_url": CONTEXT.base_url,
                "api_prefix": API_PREFIX,
                "cookies": cookies,
                "stop": self._stop,
                "hooks": self._stream_hooks(),
                "forced_disconnect_seconds": random.uniform(150, 240),
            }
            if kind == "sse":
                run_sse(**common)
            else:
                run_query_websocket(parser_type=self.parser, **common)
        self._greenlets.append(gevent.spawn(start))

    def _simple_get(self, path: str, name: str, **kwargs) -> bool:
        response = self.client.get(_api(path), name=name, **kwargs)
        return response.status_code < 400

    def _heartbeat(self) -> bool:
        response = self.client.post(
            _api("/presence/heartbeat"),
            json={"client_id": self.device_id},
            name="poll.heartbeat",
        )
        return response.status_code < 400

    def _unread(self) -> bool:
        return self._simple_get("/notifications/unread-count", "poll.unread")

    def _identity(self) -> bool:
        return self._simple_get("/auth/me", "poll.identity")

    def _maintenance(self) -> bool:
        return self._simple_get("/maintenance/status", "poll.maintenance")

    def _query_version(self) -> bool:
        return self._simple_get(f"/query/{_encoded(self.parser)}/version", "poll.query_version")

    def _query_data(self) -> bool:
        response = self.client.get(
            _api(f"/query/{_encoded(self.parser)}"),
            params={"page": 1, "page_size": 200},
            name="poll.query_data",
        )
        return response.status_code < 400

    def _visible_task_refresh(self) -> bool:
        response = self.client.get(
            _api(f"/mobile-tasks/{_encoded(self.parser)}"),
            params={"scope": self._scope(), "status": "all", "page": 1, "page_size": 20},
            name="poll.task_list",
        )
        return response.status_code < 400

    def _inline_editors(self) -> bool:
        keys = [str(item["row_key"]) for item in self._tasks(parser=self.parser)[:20]]
        if not keys:
            return True
        response = self.client.post(
            _api(f"/mobile-tasks/{_encoded(self.parser)}/inline-editors"),
            json={"row_keys": keys},
            name="poll.inline_editors",
        )
        return response.status_code < 400

    def _property_search(self) -> bool:
        response = self.client.post(
            _api("/registry/properties/search"),
            json={
                "keyword": RUNTIME.property_search_keyword,
                "status": "active",
                "page": 1,
                "page_size": 20,
            },
            name="poll.property_search",
        )
        return response.status_code < 400

    def _source(self, target: dict, *, detail_name: str = "core.detail") -> tuple[int, int, dict] | None:
        parser = str(target["parser_type"])
        row_key = str(target["row_key"])
        response = self.client.get(
            _api(f"/mobile-tasks/{_encoded(parser)}/{_encoded(row_key)}"),
            name=detail_name,
        )
        if not response.ok:
            return None
        try:
            payload = response.json()
            sources = payload.get("sources") or payload.get("task", {}).get("sources") or []
            source = sources[0]
            return int(source.get("id") or source.get("source_id")), int(source.get("revision") or 0), source
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return None

    @task(20)
    def core_list_and_detail(self) -> None:
        if not self.ready:
            return
        response = self.client.get(
            _api(f"/mobile-tasks/{_encoded(self.parser)}"),
            params={"scope": self._scope(), "status": "all", "page": 1, "page_size": 20},
            name="core.list",
        )
        if not response.ok:
            return
        try:
            rows = response.json().get("data") or response.json().get("items") or []
            if rows:
                row_key = str(rows[0].get("row_key") or "")
                if row_key:
                    self.client.get(
                        _api(f"/mobile-tasks/{_encoded(self.parser)}/{_encoded(row_key)}"),
                        name="core.detail",
                    )
        except (ValueError, AttributeError):
            return

    @task(8)
    def core_save(self) -> None:
        if not self.ready:
            return
        candidates = self._tasks("assigned")
        if not candidates:
            return
        target = random.choice(candidates)
        source_info = self._source(target, detail_name="core.detail.prepare_save")
        if not source_info:
            return
        source_id, revision, source = source_info
        self._write_index += 1
        field = str(target.get("editable_field") or "现住址")
        value = f"预发布演练-{CONTEXT.run_id}-{self.account_index:03d}-{self._write_index:05d}"
        with self.client.patch(
            _api(f"/mobile-tasks/{_encoded(str(target['parser_type']))}/source-rows/{source_id}"),
            json={
                "changes": {field: value},
                "base_values": source.get("values") or {},
                "expected_revision": revision,
            },
            name="core.save",
            catch_response=True,
        ) as response:
            if response.status_code == 409:
                GLOBAL_METRICS.observe_expected_conflict()
                response.success()
            elif response.status_code >= 400:
                response.failure(f"save {response.status_code}")

    @task(2)
    def core_claim(self) -> None:
        if not self.ready or self.account.role != "member":
            return
        candidates = self._tasks("unassigned")
        if not candidates:
            return
        target = random.choice(candidates)
        source_info = self._source(target, detail_name="core.detail.prepare_claim")
        if not source_info:
            return
        source_id, revision, source = source_info
        self._write_index += 1
        with self.client.post(
            _api(f"/mobile-tasks/{_encoded(str(target['parser_type']))}/source-rows/{source_id}/claim"),
            json={
                "changes": {"现住址": f"预发布领取-{CONTEXT.run_id}-{self.account_index:03d}-{self._write_index:05d}"},
                "base_values": source.get("values") or {},
                "expected_revision": revision,
            },
            name="core.claim",
            catch_response=True,
        ) as response:
            if response.status_code == 409:
                GLOBAL_METRICS.observe_expected_conflict()
                response.success()
            elif response.status_code >= 400:
                response.failure(f"claim {response.status_code}")

    @task(1)
    def core_assign(self) -> None:
        if not self.ready or self.account.role not in {"leader", "admin", "super_admin"}:
            return
        candidates = self._tasks("assignable")
        if not candidates:
            return
        seed = random.choice(candidates)
        parser = str(seed["parser_type"])
        community = str(seed.get("community") or "")
        inspector = str(seed.get("assignment_inspector") or "")
        rows = [
            str(item["row_key"]) for item in candidates
            if str(item["parser_type"]) == parser
            and str(item.get("community") or "") == community
            and str(item.get("assignment_inspector") or "") == inspector
        ][:4]
        if not rows or not inspector:
            return
        with self.client.post(
            _api(f"/mobile-tasks/{_encoded(parser)}/bulk-assign"),
            json={"row_keys": rows, "inspector": inspector, "mode": "single"},
            name="core.assign",
            catch_response=True,
        ) as response:
            if response.status_code == 409:
                GLOBAL_METRICS.observe_expected_conflict()
                response.success()
            elif response.status_code >= 400:
                response.failure(f"assign {response.status_code}")
