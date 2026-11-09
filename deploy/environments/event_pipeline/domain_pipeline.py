"""Static contracts and run planning for business event-pipeline domains."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


_DEV_RUN_RE = re.compile(r"^dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_STAGING_RUN_RE = re.compile(r"^STG-[0-9]{8}-[0-9]{2}$")
ENVIRONMENTS = ("development", "staging")


@dataclass(frozen=True)
class KafkaTopicSpec:
    role: str
    development: str
    staging: str
    schema_contract: str | None
    schema_versions: tuple[int, ...] = ()
    provision_environments: tuple[str, ...] = ENVIRONMENTS

    def name(self, environment: str) -> str:
        if environment == "development":
            return self.development
        if environment == "staging":
            return self.staging
        raise ValueError("non-Production pipeline topic required")

    def provisioned(self, environment: str) -> bool:
        return environment in self.provision_environments


@dataclass(frozen=True)
class FlinkSourceSpec:
    table: str
    topic_role: str
    group_suffix: str


@dataclass(frozen=True)
class DomainPipelineSpec:
    domain_id: str
    event_types: tuple[str, ...]
    topics: tuple[KafkaTopicSpec, ...]
    stages: tuple[str, ...]
    flink_sources: tuple[FlinkSourceSpec, ...] = ()
    flink_schema_versions: tuple[int, ...] = ()
    flink_sinks: tuple[str, ...] = ()
    redis_transport_identity: str | None = None
    redis_read_model_identity: str | None = None
    readiness: tuple[str, ...] = ()
    schema_result_key: str | None = None

    def topic(self, environment: str, role: str) -> str:
        matches = [item for item in self.topics if item.role == role]
        if len(matches) != 1:
            raise ValueError(f"domain topic role is not unique: {self.domain_id}:{role}")
        return matches[0].name(environment)


TASK_READ_MODEL = DomainPipelineSpec(
    domain_id="instruction_audit",
    event_types=(
        "task.created", "task.saved", "task.claimed", "task.assigned",
        "task.reviewed", "task.archived", "task.deleted",
        "photo.requested", "photo.attached", "venue.visited", "external.stats.updated",
        "task.view_state.changed", "task.view_state.removed",
        "online.task.view_state_changed",
    ),
    topics=(
        KafkaTopicSpec(
            "events", "dev.task.read-model.v4", "staging.task.read-model.v4",
            "task_read_model", (4, 5, 6),
        ),
        KafkaTopicSpec(
            "dlq", "dev.task.read-model.dlq.v4", "staging.task.read-model.dlq.v4",
            "task_read_model", (4, 5, 6),
        ),
        KafkaTopicSpec(
            "legacy_events", "dev.task.events.v1", "staging.task.events.v1",
            None, provision_environments=("staging",),
        ),
        KafkaTopicSpec(
            "legacy_dlq", "dev.task.events.dlq.v1", "staging.task.events.dlq.v1",
            None, provision_environments=("staging",),
        ),
    ),
    stages=("outbox", "relay", "redis_stream", "kafka", "flink", "redis", "api"),
    flink_sources=(
        FlinkSourceSpec("dev_events", "events", "-flink"),
        FlinkSourceSpec("dev_task_events_redis", "events", "-flink-redis"),
    ),
    flink_schema_versions=(4, 6),
    flink_sinks=("task-read-model-redis",),
    redis_transport_identity="backend",
    redis_read_model_identity="backend",
    readiness=("kafka", "flink_job", "consumer_groups", "redis_identity", "read_model_watermark"),
    schema_result_key="task",
)

ONLINE_SUMMARY_FACTS = DomainPipelineSpec(
    domain_id="online_summary_facts",
    event_types=("summary.task.changed", "summary.task.deleted"),
    topics=(
        KafkaTopicSpec(
            "events", "dev.online-summary.facts.v1", "staging.online-summary.facts.v1",
            "online_summary_fact", (1,),
        ),
        KafkaTopicSpec(
            "dlq", "dev.online-summary.facts.dlq.v1", "staging.online-summary.facts.dlq.v1",
            "online_summary_fact", (1,),
        ),
    ),
    stages=("outbox", "relay", "redis_stream", "kafka"),
    redis_transport_identity="backend",
    readiness=("outbox_relay", "kafka_delivery"),
    schema_result_key="online_summary",
)

DOMAIN_PIPELINES = (TASK_READ_MODEL, ONLINE_SUMMARY_FACTS)
_BY_ID = {item.domain_id: item for item in DOMAIN_PIPELINES}


def validate_registry(specs: Iterable[DomainPipelineSpec] = DOMAIN_PIPELINES) -> None:
    items = tuple(specs)
    if not items or len({item.domain_id for item in items}) != len(items):
        raise ValueError("domain pipeline IDs must be unique")
    seen_events: set[str] = set()
    seen_topics: set[str] = set()
    seen_sources: set[str] = set()
    seen_groups: set[str] = set()
    seen_sinks: set[str] = set()
    seen_schema_result_keys: set[str] = set()
    for spec in items:
        if not spec.domain_id or not spec.event_types or not spec.topics or not spec.stages:
            raise ValueError("domain pipeline contract is incomplete")
        if not spec.schema_result_key or spec.schema_result_key in seen_schema_result_keys:
            raise ValueError("domain schema result keys must be unique and complete")
        seen_schema_result_keys.add(spec.schema_result_key)
        duplicate_roles = len({topic.role for topic in spec.topics}) != len(spec.topics)
        if duplicate_roles:
            raise ValueError(f"domain topic roles must be unique: {spec.domain_id}")
        for event_type in spec.event_types:
            if event_type in seen_events:
                raise ValueError("domain event types must be unique")
            seen_events.add(event_type)
        for topic in spec.topics:
            if (topic.schema_contract is None) != (not topic.schema_versions):
                raise ValueError(f"Kafka schema contract is incomplete: {spec.domain_id}:{topic.role}")
            if any(type(version) is not int or version < 1 for version in topic.schema_versions):
                raise ValueError("Kafka schema versions must be positive integers")
            for environment in ENVIRONMENTS:
                name = topic.name(environment)
                if name in seen_topics:
                    raise ValueError("domain Kafka topics must be unique")
                seen_topics.add(name)
        for source in spec.flink_sources:
            if source.table in seen_sources or not source.group_suffix:
                raise ValueError("Flink source identities must be unique and complete")
            spec.topic("development", source.topic_role)
            seen_sources.add(source.table)
            group_key = (source.group_suffix, spec.domain_id)
            if group_key in seen_groups:
                raise ValueError("Flink consumer group roles must be unique")
            seen_groups.add(group_key)
        for sink in spec.flink_sinks:
            if sink in seen_sinks:
                raise ValueError("Flink sink identities must be unique")
            seen_sinks.add(sink)
        if ("redis_stream" in spec.stages) != bool(spec.redis_transport_identity):
            raise ValueError(f"Redis transport identity is incomplete: {spec.domain_id}")
        if ("redis" in spec.stages) != bool(spec.redis_read_model_identity):
            raise ValueError(f"Redis read-model identity is incomplete: {spec.domain_id}")
        if bool(spec.flink_sources) != ("flink" in spec.stages):
            raise ValueError(f"Flink stage contract is incomplete: {spec.domain_id}")
        if bool(spec.flink_schema_versions) != bool(spec.flink_sources):
            raise ValueError(f"Flink schema versions are incomplete: {spec.domain_id}")


def domain_spec(domain_id: str) -> DomainPipelineSpec:
    try:
        return _BY_ID[domain_id]
    except KeyError:
        raise ValueError("unknown domain pipeline") from None


def domain_for_event(event_type: str) -> DomainPipelineSpec:
    matches = [spec for spec in DOMAIN_PIPELINES if event_type in spec.event_types]
    if len(matches) != 1:
        raise ValueError("event type has no unique pipeline domain")
    return matches[0]


def validate_run_identity(environment: object, run_id: object) -> tuple[str, str]:
    env = str(environment or "")
    run = str(run_id or "")
    if env == "development" and _DEV_RUN_RE.fullmatch(run):
        return env, run
    if env == "staging" and _STAGING_RUN_RE.fullmatch(run):
        return env, run
    raise ValueError("non-Production pipeline identity required")


@dataclass(frozen=True)
class PipelineRunPlan:
    environment: str
    run_id: str
    kafka_topics: tuple[str, ...]
    flink_sources: dict[str, dict[str, str]]
    flink_sinks: tuple[str, ...]
    consumer_groups: tuple[str, ...]
    flink_schema_versions: tuple[int, ...]
    readiness_by_domain: dict[str, tuple[str, ...]]


class DomainPipelineOrchestrator:
    """Resolve and validate fixed domain contracts for one non-Production run."""

    def __init__(self, environment: str, run_id: str):
        validate_registry()
        self.environment, self.run_id = validate_run_identity(environment, run_id)

    def plan(self) -> PipelineRunPlan:
        provisioned_topics = sorted({
            topic.name(self.environment)
            for spec in DOMAIN_PIPELINES
            for topic in spec.topics
            if topic.provisioned(self.environment)
        })
        sources: dict[str, dict[str, str]] = {}
        sinks: set[str] = set()
        groups: set[str] = set()
        schema_versions: set[int] = set()
        for spec in DOMAIN_PIPELINES:
            for source in spec.flink_sources:
                group = self.run_id + source.group_suffix
                if source.table in sources or group in groups:
                    raise ValueError("resolved Flink source or consumer group is ambiguous")
                sources[source.table] = {
                    "consumer_group": group,
                    "topic": spec.topic(self.environment, source.topic_role),
                    "domain_id": spec.domain_id,
                }
                groups.add(group)
            sinks.update(spec.flink_sinks)
            if spec.flink_sources:
                schema_versions.update(spec.flink_schema_versions)
        return PipelineRunPlan(
            environment=self.environment,
            run_id=self.run_id,
            kafka_topics=tuple(provisioned_topics),
            flink_sources=sources,
            flink_sinks=tuple(sorted(sinks)),
            consumer_groups=tuple(sorted(groups)),
            flink_schema_versions=tuple(sorted(schema_versions)),
            readiness_by_domain={spec.domain_id: spec.readiness for spec in DOMAIN_PIPELINES},
        )

    def readiness_report(self, domain_id: str, completed_checks: Iterable[str]) -> dict[str, object]:
        spec = domain_spec(domain_id)
        completed = set(completed_checks)
        missing = sorted(set(spec.readiness) - completed)
        return {
            "domain_id": spec.domain_id,
            "stages": list(spec.stages),
            "ready": not missing,
            "missing_checks": missing,
        }


validate_registry()


__all__ = [
    "DOMAIN_PIPELINES", "DomainPipelineOrchestrator", "DomainPipelineSpec",
    "FlinkSourceSpec", "KafkaTopicSpec", "ONLINE_SUMMARY_FACTS",
    "PipelineRunPlan", "TASK_READ_MODEL", "domain_for_event", "domain_spec",
    "validate_registry", "validate_run_identity",
]
