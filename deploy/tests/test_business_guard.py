import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/kafka-shadow/business_guard.py"
sys.path.insert(0, str(SCRIPT.parent))

from business_guard import (  # noqa: E402
    BusinessGuardError,
    validate_saved_identities,
)


RUN_ID = "KSHADOW-20260907T031500Z-clean03"
PROJECT = "binhu-kafka-shadow-20260907-clean"
PROJECT_ROOT = "/srv/binhu-eventbus-shadow-20260907-clean"
NETWORK = f"{PROJECT}-network"
DATABASES = {
    "online": "KShadow_Online_clean03",
    "archive": "KShadow_Archive_clean03",
    "daily": "KShadow_Daily_clean03",
}
IMAGES = {
    "backend": "docker.1panel.live/binhu/backend@sha256:" + "a" * 64,
    "shadow-gateway": "docker.1panel.live/library/nginx@sha256:" + "b" * 64,
    "derived-mysql": "docker.1panel.live/library/mysql@sha256:" + "c" * 64,
}


def _container(service: str, *, mount=None, networks=None, image=None):
    labels = {
        "com.docker.compose.project": PROJECT,
        "com.docker.compose.service": service,
        "com.docker.compose.project.working_dir": PROJECT_ROOT,
        "binhu.shadow": "true",
        "binhu.shadow.run_id": RUN_ID,
        "binhu.shadow.scope": "business",
    }
    return {
        "Name": f"/{PROJECT}-{service}-1",
        "Config": {
            "Labels": labels,
            "Image": image or IMAGES[service],
        },
        "Mounts": [] if mount is None else [mount],
        "HostConfig": {"PortBindings": {}},
        "NetworkSettings": {
            "Networks": {name: {} for name in (networks or [NETWORK])},
            "Ports": {"37125/tcp": None},
        },
    }


def _with_real_docker_image_shape(docker):
    for index, container in enumerate(docker["containers"], start=1):
        container["Image"] = "sha256:" + f"{index:064x}"
    return docker


def valid_docker_identity():
    containers = [
        _container(
            "backend",
            mount={
                "Type": "bind",
                "Source": f"{PROJECT_ROOT}/config.env",
                "Destination": "/run/config.env",
                "RW": False,
            },
        ),
        _container(
            "shadow-gateway",
            mount={
                "Type": "bind",
                "Source": f"{PROJECT_ROOT}/shadow-gateway.conf",
                "Destination": "/etc/nginx/conf.d/default.conf",
                "RW": False,
            },
        ),
        _container(
            "derived-mysql",
            mount={
                "Type": "volume",
                "Name": f"{PROJECT}_kafka-business-mysql-data",
                "Destination": "/var/lib/mysql",
                "RW": True,
            },
        ),
    ]
    return {
        "run_id": RUN_ID,
        "project": PROJECT,
        "root": PROJECT_ROOT,
        "network": NETWORK,
        "databases": DATABASES,
        "images": dict(IMAGES),
        "containers": containers,
        "network_inspect": {
            "Name": NETWORK,
            "Internal": True,
            "Labels": {
                "com.docker.compose.project": PROJECT,
                "com.docker.compose.network": "internal",
            },
            "Containers": {
                item["Name"].lstrip("/"): {}
                for item in containers
            },
        },
    }


def valid_db_identity():
    return {
        "run_id": RUN_ID,
        "project": PROJECT,
        "databases": [
            {
                "name": name,
                "markers": [
                    {
                        "environment": "shadow",
                        "run_id": RUN_ID,
                        "database_name": name,
                    }
                ],
            }
            for name in DATABASES.values()
        ],
    }


def test_valid_saved_docker_and_db_identities_are_accepted():
    result = validate_saved_identities(RUN_ID, valid_docker_identity(), valid_db_identity())
    assert result.run_id == RUN_ID
    assert result.databases == DATABASES


def test_real_docker_inspect_local_image_id_does_not_override_config_digest():
    docker = _with_real_docker_image_shape(valid_docker_identity())
    result = validate_saved_identities(RUN_ID, docker, valid_db_identity())
    assert result.services["backend"] == IMAGES["backend"]


def test_unpinned_top_level_image_cannot_override_config_digest():
    docker = valid_docker_identity()
    docker["containers"][0]["Image"] = "docker.1panel.live/binhu/backend:latest"
    with pytest.raises(BusinessGuardError):
        validate_saved_identities(RUN_ID, docker, valid_db_identity())


def test_deployment_identity_service_and_volume_shape_is_accepted():
    docker = valid_docker_identity()
    docker["services"] = docker.pop("images")
    docker["volumes"] = {
        "mysql": f"{PROJECT}_kafka-business-mysql-data",
    }
    result = validate_saved_identities(RUN_ID, docker, valid_db_identity())
    assert result.project == PROJECT


def test_actual_biz_database_suffixes_and_docker_network_ids_are_accepted():
    docker = valid_docker_identity()
    docker.pop("databases", None)
    docker["network_inspect"]["Containers"] = {
        str(index): {"Name": name}
        for index, name in enumerate(docker["network_inspect"]["Containers"], start=1)
    }
    names = [
        "KShadow_20260907_biz01",
        "KShadow_20260907_biz01_archive",
        "KShadow_20260907_biz01_daily",
    ]
    db = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "databases": [
            {
                "name": name,
                "markers": [
                    {
                        "environment": "shadow",
                        "run_id": RUN_ID,
                        "database_name": name,
                    }
                ],
            }
            for name in names
        ],
    }
    result = validate_saved_identities(RUN_ID, docker, db)
    assert result.databases == {
        "online": names[0],
        "archive": names[1],
        "daily": names[2],
    }


def test_direct_marker_rows_are_accepted_as_database_identity_records():
    db = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "databases": {
            name: {
                "environment": "shadow",
                "run_id": RUN_ID,
                "database_name": name,
            }
            for name in (
                "KShadow_20260907_biz01",
                "KShadow_20260907_biz01_archive",
                "KShadow_20260907_biz01_daily",
            )
        },
    }
    docker = valid_docker_identity()
    docker.pop("databases", None)
    result = validate_saved_identities(RUN_ID, docker, db)
    assert result.databases["daily"].endswith("_daily")


def test_sql_marker_snapshot_shape_is_accepted():
    names = (
        "KShadow_20260907_biz01",
        "KShadow_20260907_biz01_archive",
        "KShadow_20260907_biz01_daily",
    )
    db = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "databases": {
            name: {"_shadow_identity": [["shadow", RUN_ID, name]]}
            for name in names
        },
    }
    docker = valid_docker_identity()
    docker.pop("databases", None)
    result = validate_saved_identities(RUN_ID, docker, db)
    assert result.databases["online"] == names[0]


def test_production_target_is_rejected_before_any_identity_is_accepted():
    docker = valid_docker_identity()
    docker["root"] = "/root/binhu"
    with pytest.raises(BusinessGuardError):
        validate_saved_identities(RUN_ID, docker, valid_db_identity())


def test_producer_service_name_and_fixture_directory_are_not_false_positive_targets():
    docker = valid_docker_identity()
    docker["containers"][0]["Mounts"][0]["Source"] = f"{PROJECT_ROOT}/load-tests/fixture.json"
    docker["containers"][0]["Config"]["Labels"]["com.docker.compose.service"] = "producer"
    docker["containers"][0]["Service"] = "producer"
    docker["images"]["producer"] = docker["images"].pop("backend")
    docker["containers"][0]["Name"] = f"/{PROJECT}-producer-1"
    docker["network_inspect"]["Containers"] = {
        item["Name"].lstrip("/"): {}
        for item in docker["containers"]
    }
    result = validate_saved_identities(RUN_ID, docker, valid_db_identity())
    assert result.project == PROJECT


def test_old_loadtest_project_is_rejected():
    docker = valid_docker_identity()
    docker["project"] = "binhu-loadtest-lt-20260907-01"
    with pytest.raises(BusinessGuardError):
        validate_saved_identities(RUN_ID, docker, valid_db_identity())


def test_wrong_run_id_in_a_container_label_is_rejected():
    docker = valid_docker_identity()
    docker["containers"][0]["Config"]["Labels"]["binhu.shadow.run_id"] = "KSHADOW-other"
    with pytest.raises(BusinessGuardError):
        validate_saved_identities(RUN_ID, docker, valid_db_identity())


def test_missing_database_marker_is_rejected():
    db = valid_db_identity()
    db["databases"][1]["markers"] = []
    with pytest.raises(BusinessGuardError):
        validate_saved_identities(RUN_ID, valid_docker_identity(), db)


def test_external_bind_mount_is_rejected():
    docker = valid_docker_identity()
    docker["containers"][0]["Mounts"][0]["Source"] = "/var/lib/binhu/prod.env"
    with pytest.raises(BusinessGuardError):
        validate_saved_identities(RUN_ID, docker, valid_db_identity())


def test_extra_network_is_rejected_even_when_the_internal_network_is_present():
    docker = valid_docker_identity()
    docker["containers"][0]["NetworkSettings"]["Networks"]["bridge"] = {}
    with pytest.raises(BusinessGuardError):
        validate_saved_identities(RUN_ID, docker, valid_db_identity())


def test_host_port_binding_is_rejected():
    docker = valid_docker_identity()
    docker["containers"][0]["HostConfig"]["PortBindings"] = {
        "37125/tcp": [{"HostIp": "0.0.0.0", "HostPort": "37125"}]
    }
    with pytest.raises(BusinessGuardError):
        validate_saved_identities(RUN_ID, docker, valid_db_identity())


def test_unpinned_image_is_rejected():
    docker = valid_docker_identity()
    docker["images"]["backend"] = "docker.1panel.live/binhu/backend:latest"
    with pytest.raises(BusinessGuardError):
        validate_saved_identities(RUN_ID, docker, valid_db_identity())


def test_cli_reads_saved_json_without_docker_or_database_calls(tmp_path):
    docker_path = tmp_path / "docker.json"
    db_path = tmp_path / "db.json"
    docker_path.write_text(json.dumps(valid_docker_identity()), encoding="utf-8")
    db_path.write_text(json.dumps(valid_db_identity()), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--run-id",
            RUN_ID,
            "--docker-identity",
            str(docker_path),
            "--db-identity",
            str(db_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "passed"
