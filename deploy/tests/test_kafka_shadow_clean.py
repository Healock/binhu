"""Static contract checks for the clean Kafka business-loop overlay."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "deploy/kafka-shadow/docker-compose.business.yml"
INIT = ROOT / "deploy/kafka-shadow/business-init.sh"
GATEWAY = ROOT / "deploy/kafka-shadow/shadow-gateway.conf"


def test_clean_overlay_has_backend_gateway_and_shared_run_scoped_database():
    config = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    services = config["services"]
    assert {"derived-mysql", "derived-redis", "backend", "shadow-gateway", "kafka-relay"} <= set(services)

    backend = services["backend"]
    assert backend["environment"]["APP_ENVIRONMENT"] == "shadow"
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
    config = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
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
