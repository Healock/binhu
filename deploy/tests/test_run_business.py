"""Contract tests for the isolated KSHADOW business runner."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/kafka-shadow/run_business.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("run_business_under_test", SCRIPT)
assert SPEC and SPEC.loader
run_business = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = run_business
SPEC.loader.exec_module(run_business)


RUN_ID = "KSHADOW-20260907-business01"
PROJECT = "binhu-kafka-shadow-20260907-business01"
NETWORK = f"{PROJECT}-network"
LOCUST_IMAGE = "docker.io/locustio/locust@sha256:" + "a" * 64


def _runtime_index(path: Path, run_id: str = RUN_ID, snapshot_hash: str | None = "b" * 64) -> None:
    body = {
        "schema_version": 1,
        "run_id": run_id,
        "fictional_only": True,
        "tasks": [
            {
                "ordinal": 1,
                "parser_type": "全链条",
                "row_key": "shadow-row-001",
                "source_id": 101,
                "initial_revision": 1,
                "scenario": "assigned",
                "community": "压测社区01",
                "inspector": "压测组员01",
                "property_id": 10,
                "property_version": 1,
                "property_candidates": [{"property_id": 10, "property_version": 1}],
            }
        ],
    }
    if snapshot_hash is not None:
        body["runtime_snapshot_sha256"] = snapshot_hash
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def _snapshot(root: Path, *, phase: str = "run") -> dict:
    body = {
        "schema_version": 1,
        "status": "passed",
        "phase": phase,
        "run_id": RUN_ID,
        "project": PROJECT,
        "root": root.as_posix(),
        "docker_identity": {"project": PROJECT, "run_id": RUN_ID},
        "db_identity": {"run_id": RUN_ID},
    }
    body["sha256"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    root = tmp_path / "shadow-root"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    locustfile = root / "load-tests" / "locustfile.py"
    locustfile.parent.mkdir()
    locustfile.write_text("# shared workload fixture\n", encoding="utf-8")
    index = artifacts / f"business-runtime-index-{RUN_ID}.json"
    _runtime_index(index)
    snapshot = _snapshot(root)
    return root, artifacts, locustfile, snapshot


class FakeProcess:
    returncode = 0

    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.timeout and len(self.wait_calls) == 1:
            raise subprocess.TimeoutExpired("docker run", timeout)
        return self.returncode

    def poll(self) -> int | None:
        return None if self.timeout else self.returncode


def _fake_image_result() -> SimpleNamespace:
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps([LOCUST_IMAGE]),
        stderr="",
    )


def test_only_supported_business_profiles_are_accepted():
    smoke = run_business.resolve_profile("smoke")
    assert (smoke.users, smoke.duration, smoke.spawn_rate) == (5, "300s", 1)
    burst = run_business.resolve_profile("burst75")
    assert (burst.users, burst.duration, burst.spawn_rate, burst.burst) == (75, "300s", 15, True)
    with pytest.raises(run_business.BusinessRunError):
        run_business.resolve_profile("mixed")
    with pytest.raises(run_business.BusinessRunError):
        run_business.resolve_profile("smoke", users=75)


def test_fixed_gateway_and_digest_are_required():
    assert run_business.validate_base_url("http://shadow-gateway:8080", PROJECT) == "http://shadow-gateway:8080"
    for value in ("https://shadow.example", "http://shadow-gateway:8081", "http://shadow-gateway:8080/api"):
        with pytest.raises(run_business.BusinessRunError):
            run_business.validate_base_url(value, PROJECT)
    assert "@sha256:" in LOCUST_IMAGE
    with pytest.raises(run_business.BusinessRunError):
        run_business.validate_locust_image("locustio/locust:latest")
    with pytest.raises(run_business.BusinessRunError):
        run_business.validate_locust_image("locustio/locust@sha256:" + "0" * 63)


def test_runtime_index_requires_schema_provenance_and_closed_fields(tmp_path):
    path = tmp_path / "runtime.json"
    _runtime_index(path)
    result = run_business.validate_runtime_index(path, RUN_ID)
    assert result["tasks"][0]["source_id"] == 101
    _runtime_index(path, snapshot_hash=None)
    missing = json.loads(path.read_text(encoding="utf-8"))
    missing.pop("runtime_snapshot_sha256", None)
    path.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(run_business.BusinessRunError):
        run_business.validate_runtime_index(path, RUN_ID)

    _runtime_index(path)
    unsafe = json.loads(path.read_text(encoding="utf-8"))
    unsafe["unknown"] = True
    path.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(run_business.BusinessRunError):
        run_business.validate_runtime_index(path, RUN_ID)

    _runtime_index(path)
    unsafe = json.loads(path.read_text(encoding="utf-8"))
    unsafe["tasks"][0]["unknown"] = True
    path.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(run_business.BusinessRunError):
        run_business.validate_runtime_index(path, RUN_ID)

    _runtime_index(path)
    unsafe = json.loads(path.read_text(encoding="utf-8"))
    unsafe["tasks"][0]["property_candidates"][0]["community"] = "不允许"
    path.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(run_business.BusinessRunError):
        run_business.validate_runtime_index(path, RUN_ID)


def test_run_collects_fresh_identity_before_docker_run_and_uses_container_boundary(tmp_path, monkeypatch):
    root, artifacts, locustfile, snapshot = _fixture(tmp_path)
    events: list[str] = []

    def collect(*args, **kwargs):
        events.append("collect")
        assert kwargs["phase"] == "run"
        return snapshot

    monkeypatch.setattr(run_business, "collect_runtime", collect)
    monkeypatch.setattr(run_business, "validate_current_root", lambda value, cwd=None: str(root))

    def guard(run_id, docker, db):
        events.append("guard")
        return SimpleNamespace(project=PROJECT, network=NETWORK)

    monkeypatch.setattr(run_business, "validate_saved_identities", guard)
    commands: list[list[str]] = []

    def docker_runner(command, **kwargs):
        commands.append(list(command))
        if command[:3] == ["docker", "image", "inspect"]:
            return _fake_image_result()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    process = FakeProcess()
    launched: list[tuple[list[str], dict]] = []

    def popen(command, **kwargs):
        events.append("docker-run")
        launched.append((list(command), kwargs))
        return process

    result = run_business.run(
        RUN_ID,
        root=root,
        runtime_index=artifacts / f"business-runtime-index-{RUN_ID}.json",
        locustfile=locustfile,
        output_dir=artifacts,
        locust_image=LOCUST_IMAGE,
        popen_factory=popen,
        docker_runner=docker_runner,
    )
    assert events == ["collect", "guard", "docker-run"]
    assert result["status"] == "completed"
    assert result["acceptance"] == "not_performed"
    assert result["network"] == NETWORK
    command = launched[0][0]
    assert command[:6] == ["docker", "run", "--pull", "never", "--network", NETWORK]
    assert LOCUST_IMAGE in command
    assert "--cap-drop" in command and command[command.index("--cap-drop") + 1] == "ALL"
    assert "--read-only" in command
    assert "--tmpfs" in command
    assert "--cpus" in command and "--memory" in command and "--pids-limit" in command
    assert "-p" not in command and "--publish" not in command
    assert not any("docker.sock" in item for item in command)
    assert "http://shadow-gateway:8080" in command
    assert not any("shadow.example" in item or "BUSINESS_SHADOW_BASE_URL" in item for item in command)
    env_values = [command[index + 1] for index, value in enumerate(command) if value == "--env"]
    assert "LOAD_TEST_RUN_ID=" + RUN_ID in env_values
    assert "SHADOW_RUNTIME_INDEX=/run-input/business-runtime-index-" + RUN_ID + ".json" in env_values
    assert any(item.startswith("SHADOW_EVENT_LOG=/run-artifacts/business-events-") for item in env_values)
    assert not any("PASSWORD" in item or "TOKEN" in item for item in env_values)
    mount_values = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
    assert any("dst=/load-tests/locustfile.py" in item and "readonly" in item for item in mount_values)
    assert any("dst=/run-artifacts" in item and "rw" in item for item in mount_values)
    container_name = command[command.index("--name") + 1]
    assert RUN_ID in container_name
    assert "binhu-loadtest" not in container_name
    assert any(call[:3] == ["docker", "image", "inspect"] for call in commands)
    assert any(call[:3] == ["docker", "rm", "-f"] and container_name in call for call in commands)


def test_guard_failure_prevents_image_inspect_and_docker_run(tmp_path, monkeypatch):
    root, artifacts, locustfile, _ = _fixture(tmp_path)
    monkeypatch.setattr(run_business, "validate_current_root", lambda value, cwd=None: str(root))
    monkeypatch.setattr(
        run_business,
        "collect_runtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(run_business.RuntimeInspectionError("identity")),
    )
    calls: list[list[str]] = []
    with pytest.raises(run_business.BusinessRunError):
        run_business.run(
            RUN_ID,
            root=root,
            runtime_index=artifacts / f"business-runtime-index-{RUN_ID}.json",
            locustfile=locustfile,
            output_dir=artifacts,
            docker_runner=lambda command, **kwargs: calls.append(list(command)),
            popen_factory=lambda *args, **kwargs: calls.append(list(args[0])),
        )
    assert calls == []


def test_timeout_only_stops_kills_and_removes_current_container(tmp_path, monkeypatch):
    root, artifacts, locustfile, snapshot = _fixture(tmp_path)
    monkeypatch.setattr(run_business, "collect_runtime", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(run_business, "validate_current_root", lambda value, cwd=None: str(root))
    monkeypatch.setattr(
        run_business,
        "validate_saved_identities",
        lambda *args: SimpleNamespace(project=PROJECT, network=NETWORK),
    )
    commands: list[list[str]] = []

    def docker_runner(command, **kwargs):
        commands.append(list(command))
        return _fake_image_result() if command[:3] == ["docker", "image", "inspect"] else SimpleNamespace(returncode=0, stdout="", stderr="")

    result = run_business.run(
        RUN_ID,
        root=root,
        runtime_index=artifacts / f"business-runtime-index-{RUN_ID}.json",
        locustfile=locustfile,
        output_dir=artifacts,
        locust_image=LOCUST_IMAGE,
        run_timeout=0.1,
        process_factory=lambda: FakeProcess(timeout=True),
        docker_runner=docker_runner,
    )
    name = result["container_name"]
    assert result["status"] == "failed"
    assert result["timed_out"] is True
    controls = [call for call in commands if call[:2] == ["docker", "stop"] or call[:2] == ["docker", "kill"] or call[:2] == ["docker", "rm"]]
    assert controls
    assert all(name in call for call in controls)
    assert all("binhu-loadtest" not in call for call in controls)


def test_start_failure_still_writes_report_and_removes_owned_container(tmp_path, monkeypatch):
    root, artifacts, locustfile, snapshot = _fixture(tmp_path)
    monkeypatch.setattr(run_business, "collect_runtime", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(run_business, "validate_current_root", lambda value, cwd=None: str(root))
    monkeypatch.setattr(
        run_business,
        "validate_saved_identities",
        lambda *args: SimpleNamespace(project=PROJECT, network=NETWORK),
    )
    commands: list[list[str]] = []

    def docker_runner(command, **kwargs):
        commands.append(list(command))
        return _fake_image_result() if command[:3] == ["docker", "image", "inspect"] else SimpleNamespace(returncode=0, stdout="", stderr="")

    def start_failure(*args, **kwargs):
        raise OSError("docker unavailable")

    result = run_business.run(
        RUN_ID,
        root=root,
        runtime_index=artifacts / f"business-runtime-index-{RUN_ID}.json",
        locustfile=locustfile,
        output_dir=artifacts,
        locust_image=LOCUST_IMAGE,
        popen_factory=start_failure,
        docker_runner=docker_runner,
    )
    assert result["status"] == "failed"
    assert result["exit_code"] == 127
    assert Path(result["container_stdout"]).is_file()
    assert Path(result["container_stderr"]).is_file()
    assert any(call[:3] == ["docker", "rm", "-f"] for call in commands)
