import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "deploy/kafka-shadow"
import sys

sys.path.insert(0, str(SCRIPT_DIR))

from business_guard import BusinessGuardError  # noqa: E402
from inspect_business_runtime import (  # noqa: E402
    RuntimeInspectionError,
    build_compose_command,
    build_marker_query_command,
    build_runtime_snapshot,
    filter_identity_env,
    parse_dotenv_identity,
    _required_env_alias,
    validate_current_root,
    parse_compose_config,
    _compose_network,
)
from business_guard import validate_network


@pytest.mark.parametrize('suffix', ['_default', '-internal', '_internal'])
def test_project_network_names_require_exact_project_scope(suffix):
    project = 'binhu-kafka-shadow-business02'
    name = project + suffix
    config = {'networks': {'internal': {'name': name, 'internal': True}}}
    assert _compose_network(config, project) == name
    assert validate_network(name, project) == name
    with pytest.raises(RuntimeInspectionError):
        _compose_network(config, project + '-other')
    with pytest.raises(BusinessGuardError):
        validate_network(name, project + '-other')


RUN_ID = "KSHADOW-20260907T031500Z-biz01"
PROJECT = "binhu-kafka-shadow-20260907-biz01"
ROOT_DIR = "/srv/binhu-eventbus-shadow-20260907-biz01"
NETWORK = f"{PROJECT}-network"
DBS = {
    "online": "KShadow_20260907_biz01",
    "archive": "KShadow_20260907_biz01_archive",
    "daily": "KShadow_20260907_biz01_daily",
}
IMAGES = {
    "backend": "docker.1panel.live/binhu/backend@sha256:" + "a" * 64,
    "shadow-gateway": "docker.1panel.live/library/nginx@sha256:" + "b" * 64,
    "derived-mysql": "docker.1panel.live/library/mysql@sha256:" + "c" * 64,
}
VOLUMES = {
    "mysql": f"{PROJECT}_kafka-business-mysql-data",
}


def _manifest():
    return {
        "project": PROJECT,
        "run_id": RUN_ID,
        "root": ROOT_DIR,
        "status": "running",
        "services": dict(IMAGES),
        "volumes": dict(VOLUMES),
    }


def _labels(service):
    return {
        "com.docker.compose.project": PROJECT,
        "com.docker.compose.service": service,
        "com.docker.compose.project.working_dir": ROOT_DIR,
        "binhu.shadow": "true",
        "binhu.shadow.run_id": RUN_ID,
        "binhu.shadow.scope": "business",
    }


def _container(service, *, env=None, mounts=None):
    return {
        "Name": f"/{PROJECT}-{service}-1",
        "State": {"Running": True},
        "Config": {
            "Image": IMAGES[service],
            "Labels": _labels(service),
            "Env": env or [],
        },
        "Mounts": mounts or [],
        "HostConfig": {"PortBindings": {}},
        "NetworkSettings": {
            "Networks": {NETWORK: {}},
            "Ports": {"37125/tcp": None},
        },
    }


def _compose():
    return {
        "name": PROJECT,
        "services": {
            "backend": {
                "image": IMAGES["backend"],
                "networks": {"internal": {}},
                "environment": {
                    "APP_ENVIRONMENT": "shadow",
                    "LOAD_TEST_RUN_ID": RUN_ID,
                    "MYSQL_ONLINE_DATA_DB": DBS["online"],
                    "MYSQL_ARCHIVE_DB": DBS["archive"],
                    "MYSQL_DAILY_REPORT_DB": DBS["daily"],
                    "BACKEND_PASSWORD": "must-not-be-archived",
                },
            },
            "shadow-gateway": {
                "image": IMAGES["shadow-gateway"],
                "networks": {"internal": {}},
            },
            "derived-mysql": {
                "image": IMAGES["derived-mysql"],
                "networks": {"internal": {}},
                "environment": {
                    "MYSQL_DATABASE": DBS["online"],
                    "BUSINESS_ARCHIVE_DATABASE": DBS["archive"],
                    "BUSINESS_DAILY_DATABASE": DBS["daily"],
                },
            },
        },
        "networks": {
            "internal": {
                "name": NETWORK,
                "internal": True,
                "labels": {
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.network": "internal",
                },
            }
        },
        "volumes": {
            "mysql": {"name": VOLUMES["mysql"]},
        },
    }


def _containers():
    return [
        _container(
            "backend",
            env=[
                f"APP_ENVIRONMENT=shadow",
                f"LOAD_TEST_RUN_ID={RUN_ID}",
                f"MYSQL_ONLINE_DATA_DB={DBS['online']}",
                f"MYSQL_ARCHIVE_DB={DBS['archive']}",
                f"MYSQL_DAILY_REPORT_DB={DBS['daily']}",
                "BACKEND_PASSWORD=must-not-be-archived",
            ],
            mounts=[
                {
                    "Type": "bind",
                    "Source": f"{ROOT_DIR}/config.env",
                    "Destination": "/run/config.env",
                    "RW": False,
                }
            ],
        ),
        _container(
            "shadow-gateway",
            mounts=[
                {
                    "Type": "bind",
                    "Source": f"{ROOT_DIR}/shadow-gateway.conf",
                    "Destination": "/etc/nginx/conf.d/default.conf",
                    "RW": False,
                }
            ],
        ),
        _container(
            "derived-mysql",
            mounts=[
                {
                    "Type": "volume",
                    "Name": VOLUMES["mysql"],
                    "Destination": "/var/lib/mysql",
                    "RW": True,
                }
            ],
        ),
    ]


def _network():
    return {
        "Name": NETWORK,
        "Internal": True,
        "Labels": {
            "com.docker.compose.project": PROJECT,
            "com.docker.compose.network": "internal",
        },
        "Containers": {
            item["Name"].lstrip("/"): {}
            for item in _containers()
        },
    }


def _markers():
    return {
        name: [("shadow", RUN_ID, name)]
        for name in DBS.values()
    }


def test_current_root_requires_the_server_shadow_namespace_and_cwd_match():
    assert validate_current_root(ROOT_DIR, ROOT_DIR) == ROOT_DIR
    with pytest.raises(RuntimeInspectionError):
        validate_current_root("/root/binhu", "/root/binhu")
    with pytest.raises(RuntimeInspectionError):
        validate_current_root(ROOT_DIR, "/srv/binhu-eventbus-shadow-other")


def test_identity_environment_filter_excludes_credentials_and_keeps_run_database_keys():
    filtered = filter_identity_env(
        {
            "APP_ENVIRONMENT": "shadow",
            "LOAD_TEST_RUN_ID": RUN_ID,
            "MYSQL_ONLINE_DATA_DB": DBS["online"],
            "BACKEND_PASSWORD": "secret",
            "DERIVED_READBACK_TOKEN": "secret",
            "ENCRYPTION_KEY": "secret",
        }
    )
    assert filtered == {
        "APP_ENVIRONMENT": "shadow",
        "LOAD_TEST_RUN_ID": RUN_ID,
        "MYSQL_ONLINE_DATA_DB": DBS["online"],
    }


def test_dotenv_identity_is_parsed_without_retaining_secret_values():
    env = parse_dotenv_identity(
        "\n".join(
            (
                f"KAFKA_RUN_ID={RUN_ID}",
                f"COMPOSE_PROJECT_NAME={PROJECT}",
                f"KAFKA_NETWORK={NETWORK}",
                "BUSINESS_MYSQL_ROOT_PASSWORD=secret",
                "# comment",
            )
        )
    )
    assert env == {
        "KAFKA_RUN_ID": RUN_ID,
        "COMPOSE_PROJECT_NAME": PROJECT,
        "KAFKA_NETWORK": NETWORK,
    }


def test_dotenv_identity_aliases_must_agree_and_be_non_empty():
    identity = {"COMPOSE_PROJECT_NAME": PROJECT, "KAFKA_PROJECT": PROJECT}
    assert _required_env_alias(
        identity,
        ("COMPOSE_PROJECT_NAME", "KAFKA_PROJECT"),
        "project",
    ) == PROJECT

    identity["KAFKA_PROJECT"] = PROJECT + "-other"
    with pytest.raises(RuntimeInspectionError):
        _required_env_alias(
            identity,
            ("COMPOSE_PROJECT_NAME", "KAFKA_PROJECT"),
            "project",
        )

    identity["KAFKA_PROJECT"] = ""
    with pytest.raises(RuntimeInspectionError):
        _required_env_alias(
            identity,
            ("COMPOSE_PROJECT_NAME", "KAFKA_PROJECT"),
            "project",
        )


def test_compose_network_labels_are_optional_until_live_network_inspect():
    config = _compose()
    config["networks"]["internal"].pop("labels")
    snapshot = build_runtime_snapshot(
        _manifest(), config, _containers(), _network(), _markers()
    )
    assert snapshot["status"] == "passed"


def test_runtime_snapshot_is_sanitized_hashed_and_guard_validated():
    snapshot = build_runtime_snapshot(
        _manifest(),
        _compose(),
        _containers(),
        _network(),
        _markers(),
        phase="run",
        collected_at="2026-09-07T04:00:00Z",
    )
    assert snapshot["status"] == "passed"
    assert snapshot["phase"] == "run"
    assert len(snapshot["sha256"]) == 64
    encoded = json.dumps(snapshot, ensure_ascii=False)
    assert "must-not-be-archived" not in encoded
    assert "BACKEND_PASSWORD" not in encoded
    assert snapshot["docker_identity"]["run_id"] == RUN_ID
    assert snapshot["db_identity"]["databases"][0]["name"] == DBS["online"]


def test_runtime_snapshot_rejects_manifest_root_project_or_run_mismatch():
    manifest = _manifest()
    manifest["root"] = "/srv/binhu-eventbus-shadow-other"
    with pytest.raises(BusinessGuardError):
        build_runtime_snapshot(manifest, _compose(), _containers(), _network(), _markers())

    manifest = _manifest()
    manifest["run_id"] = "KSHADOW-other"
    with pytest.raises(BusinessGuardError):
        build_runtime_snapshot(manifest, _compose(), _containers(), _network(), _markers())


def test_runtime_snapshot_rejects_extra_network_and_published_port():
    containers = _containers()
    containers[0]["NetworkSettings"]["Networks"]["bridge"] = {}
    with pytest.raises(BusinessGuardError):
        build_runtime_snapshot(_manifest(), _compose(), containers, _network(), _markers())

    containers = _containers()
    containers[0]["NetworkSettings"]["Ports"]["37125/tcp"] = [
        {"HostIp": "0.0.0.0", "HostPort": "37125"}
    ]
    with pytest.raises(BusinessGuardError):
        build_runtime_snapshot(_manifest(), _compose(), containers, _network(), _markers())


def test_read_only_commands_have_bounded_identity_queries_only():
    compose = build_compose_command(
        Path(ROOT_DIR),
        Path(ROOT_DIR) / ".env",
        [Path(ROOT_DIR) / "docker-compose.yml"],
        PROJECT,
    )
    assert compose[-3:] == ["config", "--format", "json"]
    assert "--env-file" in compose
    assert "up" not in compose and "start" not in compose and "stop" not in compose

    marker = build_marker_query_command("mysql-container", DBS["online"])
    assert marker[:3] == ["docker", "exec", "mysql-container"]
    assert "mysql" in marker[-1]
    assert "-N" in marker[-1] and "-B" in marker[-1]
    assert "MYSQL_ROOT_PASSWORD" in marker[-1]
    assert "docker compose" not in " ".join(marker)


def test_runtime_snapshot_rejects_missing_marker():
    markers = _markers()
    markers[DBS["daily"]] = []
    with pytest.raises(BusinessGuardError):
        build_runtime_snapshot(_manifest(), _compose(), _containers(), _network(), markers)
