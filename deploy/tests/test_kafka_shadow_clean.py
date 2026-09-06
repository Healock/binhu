"""Static contract checks for the clean Kafka business-loop overlay."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "deploy/kafka-shadow/docker-compose.business.yml"
INIT = ROOT / "deploy/kafka-shadow/business-init.sh"
GATEWAY = ROOT / "deploy/kafka-shadow/shadow-gateway.conf"


class ComposeLoader(yaml.SafeLoader):
    """Load Compose's !override tag for static checks without Docker."""


ComposeLoader.add_constructor(
    "!override", lambda loader, node: loader.construct_sequence(node)
)


def _compose():
    return yaml.load(OVERLAY.read_text(encoding="utf-8"), Loader=ComposeLoader)


def test_clean_overlay_has_backend_gateway_and_shared_run_scoped_database():
    config = _compose()
    services = config["services"]
    assert {"derived-mysql", "derived-redis", "backend", "shadow-gateway", "kafka-relay"} <= set(services)

    backend = services["backend"]
    assert backend["environment"]["APP_ENVIRONMENT"] == "shadow"
    assert backend["environment"]["APP_VERSION"] == "${BUSINESS_APP_VERSION:?read repository VERSION}"
    assert backend["environment"]["LOAD_TEST_RUN_ID"] == "${KAFKA_RUN_ID}"
    assert backend["environment"]["KAFKA_TASK_EVENTS_ENABLED"] == "true"
    assert backend["environment"]["MYSQL_HOST"] == "derived-mysql"
    assert backend["environment"]["MYSQL_ONLINE_DATA_DB"] == "${BUSINESS_ONLINE_DATABASE}"
    assert backend["environment"]["MYSQL_ARCHIVE_DB"] == "${BUSINESS_ARCHIVE_DATABASE}"
    assert backend["environment"]["MYSQL_DAILY_REPORT_DB"] == "${BUSINESS_DAILY_DATABASE}"
    assert backend["environment"]["REDIS_URL"].startswith("redis://")
    assert backend["networks"] == ["internal"]
    assert not backend.get("ports")

    gateway = services["shadow-gateway"]
    assert gateway["user"] == "101:101"
    assert gateway["networks"] == ["internal"]
    assert not gateway.get("ports")
    assert gateway["depends_on"]["backend"]["condition"] == "service_healthy"

    relay = services["kafka-relay"]
    assert relay["depends_on"]["derived-mysql"]["condition"] == "service_healthy"
    assert relay["environment"]["MYSQL_DATABASE"] == "${BUSINESS_ONLINE_DATABASE}"


def test_clean_init_creates_identity_delivery_ledger_and_backend_user():
    source = INIT.read_text(encoding="utf-8")
    for required in (
        "_shadow_identity",
        "_kafka_event_delivery",
        "KAFKA_RUN_ID",
        "SHADOW_BACKEND_USER",
        "BUSINESS_ARCHIVE_DATABASE",
        "BUSINESS_DAILY_DATABASE",
        "CREATE TABLE IF NOT EXISTS $archive_db._shadow_identity",
        "CREATE TABLE IF NOT EXISTS $daily_db._shadow_identity",
        "GRANT ALL PRIVILEGES",
    ):
        assert required in source
    assert "OnlineData" in source
    assert "mysql://" not in source.lower()


def test_shadow_gateway_rewrites_only_fixed_shadow_api_prefix():
    source = GATEWAY.read_text(encoding="utf-8")
    assert "location ^~ /shadow-api/" in source
    assert "rewrite ^/shadow-api/(.*)$ /api/$1 break" in source
    assert "proxy_pass http://backend:37125" in source
    assert "location /" not in source


def test_business_overlay_keeps_every_service_internal_and_enables_relay():
    config = _compose()
    for service in config["services"].values():
        assert service["networks"] == ["internal"]
        assert not service.get("ports")
        assert service.get("labels", {}).get("binhu.shadow.scope") == "business"
    assert config["services"]["kafka-relay"].get("profiles") == []
    assert set(config["volumes"]) == {
        "kafka-business-mysql-data", "kafka-business-redis-data",
    }
    for service in ("backend", "shadow-gateway", "kafka-relay"):
        assert "@sha256:" in config["services"][service]["image"] or "?set" in config["services"][service]["image"]


def test_business_overlay_overrides_derived_mounts_and_schema_path_is_explicit():
    source = OVERLAY.read_text(encoding="utf-8")
    assert "volumes: !override" in source
    assert "profiles: !override []" in source
    assert "${BUSINESS_SCHEMA_PATH:?" in source
    assert "../backend/init.sql" not in source
    assert "./derived-init.sql" not in source
    assert "./derived-redis.conf" not in source


def test_business_backend_keeps_the_original_75_user_runtime_profile():
    services = _compose()["services"]
    mysql = services["derived-mysql"]
    assert mysql["command"] == ["mysqld", "--max_connections=151", "--innodb_buffer_pool_size=1G"]
    assert mysql["cpus"] == 4
    assert mysql["mem_limit"] == "3g"
    backend = services["backend"]
    env = backend["environment"]
    assert env["MYSQL_DOMAIN_DATABASES_ENABLED"] == "true"
    assert env["PLATFORM_DOMAIN_ACTIVE"] == "true"
    assert env["DAILY_DOMAIN_ACTIVE"] == "true"
    assert env["ONLINE_PROJECTION_WORKER_CONCURRENCY"] == "3"
    assert env["ONLINE_PROJECTION_CLAIM_LIMIT"] == "100"
    assert env["ONLINE_PROJECTION_MICRO_BATCH_SIZE"] == "25"
    assert env["DERIVED_READBACK_TOKEN"] == "${BUSINESS_DERIVED_READBACK_TOKEN:?set isolated readback token}"
    assert backend["cpus"] == 2
    assert backend["mem_limit"] == "768m"
    assert backend["pids_limit"] == 256


def test_business_initializer_isolated_and_fail_closed():
    source = INIT.read_text(encoding="utf-8")
    assert "(\nset -Eeuo pipefail" in source
    assert "COUNT(*) FROM ${marker_db}._shadow_identity" in source
    assert "DELETE FROM $online_db._shadow_identity" not in source
    assert "DELETE FROM $archive_db._shadow_identity" not in source
    assert "DELETE FROM $daily_db._shadow_identity" not in source
    assert "DROP INDEX uk_row_key,/s/DROP INDEX uk_row_key,//" in source
    assert "s/daily_report/${daily_db}/g" not in source
    assert "UPDATE ${online_db}._backup_schedule SET enabled=0, next_run_at=NULL WHERE id=1" in source
