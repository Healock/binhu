"""Dedicated concentrated-login scenario; no business traffic is mixed in."""

from __future__ import annotations

import hashlib
import itertools
import threading

import time

from locust import HttpUser, between, task


_counter = itertools.count()
_lock = threading.Lock()


def _accounts() -> list[str]:
    names: list[str] = []
    for role, count in (("member", 35), ("leader", 8), ("internal_business", 4), ("admin", 2), ("super_admin", 1)):
        names.extend(f"loadtest-{role}-{index:02d}" for index in range(1, count + 1))
    return names


class ConcentratedLoginUser(HttpUser):
    wait_time = between(60, 60)

    def on_start(self) -> None:
        with _lock:
            self.index = next(_counter)
        username = _accounts()[self.index % 50]
        password = f"LoadTest-{hashlib.sha256(username.encode()).hexdigest()[:16]}!"
        with self.client.post(
            "/shadow-api/auth/login",
            json={"username": username, "password": password, "device_type": "windows",
                  "device_id": f"login-only-{self.index:03d}"},
            name="POST /shadow-api/auth/login concentrated",
            catch_response=True,
        ) as response:
            if response.status_code >= 400:
                response.failure(f"login {response.status_code}")

    @task
    def hold_session(self) -> None:
        # Keep the user alive after the single on_start login.  Ending it here
        # makes Locust spawn a replacement user, turning a 50-account burst
        # into an unbounded stream of logins.
        time.sleep(60)
