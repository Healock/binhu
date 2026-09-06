import importlib.util
from pathlib import Path
import pytest

path = Path(__file__).resolve().parents[1] / "kafka-shadow/relay_runtime.py"
spec = importlib.util.spec_from_file_location("relay_runtime", path)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def config():
    return dict(APP_ENVIRONMENT="shadow", LOAD_TEST_RUN_ID="KSHADOW-test",
                MYSQL_DATABASE="KShadow_test", MYSQL_HOST="derived-mysql",
                MYSQL_USER="shadow_derived", MYSQL_PASSWORD="fictional-credential-for-tests-only",
                KAFKA_BOOTSTRAP_SERVERS="kafka-1:9092,kafka-2:9092,kafka-3:9092")


@pytest.mark.parametrize("key,value", [
    ("APP_ENVIRONMENT", "production"), ("LOAD_TEST_RUN_ID", ""),
    ("MYSQL_DATABASE", "OnlineData"), ("MYSQL_DATABASE", "mysql"),
    ("MYSQL_HOST", "mysql"), ("MYSQL_USER", "root"),
    ("KAFKA_BOOTSTRAP_SERVERS", "external.example:9092"), ("MYSQL_PASSWORD", "short"),
])
def test_invalid_target_rejected_before_driver_import(key, value):
    env = config()
    env[key] = value
    with pytest.raises(ValueError):
        runtime.configuration(env)


def test_shadow_configuration_validates_without_side_effects():
    result = runtime.configuration(config())
    assert result["database"] == "KShadow_test"
    assert result["run_id"] == "KSHADOW-test"


def test_overlay_has_single_bounded_tmpfs_and_no_published_ports():
    import yaml
    config = yaml.safe_load((path.parent / "docker-compose.derived.yml").read_text())
    assert config["services"]["kafka-relay"]["tmpfs"] == ["/tmp:size=16m,mode=1777"]
    for service in config["services"].values():
        assert not service.get("ports")
        assert service["networks"] == ["internal"]
        assert service["mem_limit"] and service["pids_limit"]
    assert all(not value for value in config["volumes"].values())
