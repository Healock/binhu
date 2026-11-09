import pytest

from deploy.environments.event_pipeline.domain_pipeline import (
    DOMAIN_PIPELINES,
    DomainPipelineOrchestrator,
    DomainPipelineSpec,
    FlinkSourceSpec,
    KafkaTopicSpec,
    ONLINE_SUMMARY_FACTS,
    TASK_READ_MODEL,
    domain_for_event,
    validate_registry,
)


def test_registry_defines_current_task_and_summary_fact_stages():
    validate_registry()

    task = DomainPipelineOrchestrator("development", "dev-contract").plan()
    assert task.flink_sources == {
        "dev_events": {
            "consumer_group": "dev-contract-flink",
            "topic": "dev.task.read-model.v4",
            "domain_id": "instruction_audit",
        },
        "dev_task_events_redis": {
            "consumer_group": "dev-contract-flink-redis",
            "topic": "dev.task.read-model.v4",
            "domain_id": "instruction_audit",
        },
    }
    assert task.flink_sinks == ("task-read-model-redis",)
    assert task.flink_schema_versions == (4, 6)
    assert "dev.online-summary.facts.v1" in task.kafka_topics

    summary = next(spec for spec in DOMAIN_PIPELINES if spec.domain_id == "online_summary_facts")
    assert summary.stages == ("outbox", "relay", "redis_stream", "kafka")
    assert not summary.flink_sources
    assert not summary.flink_sinks
    assert summary.redis_transport_identity == "backend"
    assert summary.redis_read_model_identity is None


def test_staging_plan_uses_staging_topics_and_keeps_compat_topics_provisioned():
    plan = DomainPipelineOrchestrator("staging", "STG-20261109-01").plan()
    assert plan.flink_sources["dev_events"]["topic"] == "staging.task.read-model.v4"
    assert "staging.online-summary.facts.v1" in plan.kafka_topics
    assert "staging.task.events.v1" in plan.kafka_topics
    assert not any(topic.startswith("dev.") or topic.startswith("dev-") for topic in plan.kafka_topics)


@pytest.mark.parametrize(
    "environment,run_id",
    [
        ("development", "STG-20261109-01"),
        ("staging", "dev-contract"),
        ("production", "PRD-20261109-01"),
    ],
)
def test_orchestrator_rejects_invalid_environment_identity(environment, run_id):
    with pytest.raises(ValueError, match="identity"):
        DomainPipelineOrchestrator(environment, run_id)


def test_registry_rejects_duplicate_topics_events_and_incomplete_stage_contracts():
    duplicate_event = DomainPipelineSpec(
        domain_id="other", event_types=TASK_READ_MODEL.event_types,
        topics=(KafkaTopicSpec("events", "other.dev", "other.staging", "other"),),
        stages=("kafka",), schema_result_key="other",
    )
    with pytest.raises(ValueError, match="event types"):
        validate_registry((*DOMAIN_PIPELINES, duplicate_event))

    duplicate_topic = DomainPipelineSpec(
        domain_id="other", event_types=("other.event",),
        topics=(KafkaTopicSpec(
            "events", "dev.task.read-model.v4", "other.staging", "other", (1,),
        ),),
        stages=("kafka",), schema_result_key="other",
    )
    with pytest.raises(ValueError, match="topics"):
        validate_registry((*DOMAIN_PIPELINES, duplicate_topic))

    incomplete_flink = DomainPipelineSpec(
        domain_id="other", event_types=("other.event",),
        topics=(KafkaTopicSpec("events", "other.dev", "other.staging", "other", (1,)),),
        stages=("kafka", "flink"),
        flink_sources=(FlinkSourceSpec("other_source", "missing", "-other"),),
        schema_result_key="other",
    )
    with pytest.raises(ValueError, match="topic role"):
        validate_registry((*DOMAIN_PIPELINES, incomplete_flink))


def test_event_types_resolve_to_their_registered_business_domain():
    from deploy.environments.event_pipeline.services.kafka_event_contract import EVENT_TYPES

    assert EVENT_TYPES <= set(TASK_READ_MODEL.event_types)
    assert domain_for_event("task.saved") is TASK_READ_MODEL
    assert domain_for_event("summary.task.changed") is ONLINE_SUMMARY_FACTS


def test_readiness_report_does_not_confuse_summary_kafka_delivery_with_a_redis_view():
    orchestrator = DomainPipelineOrchestrator("development", "dev-contract")
    summary = orchestrator.readiness_report(
        "online_summary_facts", ("outbox_relay", "kafka_delivery"),
    )
    assert summary["ready"] is True
    assert "flink" not in summary["stages"]
    assert "redis" not in summary["stages"]

    task = orchestrator.readiness_report("instruction_audit", ("kafka", "flink_job"))
    assert task["ready"] is False
    assert task["missing_checks"] == [
        "consumer_groups", "read_model_watermark", "redis_identity",
    ]
