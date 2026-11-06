"""Fixed internal Dev Schema Registry contract; no caller-supplied URLs."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import urllib.request

from .services.kafka_event_contract import (
    V4_EVENT_FIELDS, V5_REQUIRED_FIELDS, V6_REQUIRED_FIELDS,
    EVENT_TYPES, CHANGED_FIELDS, SOURCE_TABLE_NAMES,
)
from .identity import topic_for, summary_topic_for

# The deployed registry is Apicurio 2.x. Its Confluent-compatible API is
# explicitly namespaced; 8081 belongs to neither this service nor this API.
BASE = "http://schema-registry:8080/apis/ccompat/v7"
SUBJECT = "dev.task.read-model.v4-value"


def subject_for(environment: str) -> str:
    return topic_for(environment) + "-value"


def summary_subject_for(environment: str) -> str:
    return summary_topic_for(environment) + "-value"


def schema(environment: str = "development"):
    topic_for(environment)
    run_pattern = (
        "^dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
        if environment == "development" else "^STG-[0-9]{8}-[0-9]{2}$"
    )
    uuid = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
    contract = {"$schema": "http://json-schema.org/draft-07/schema#", "title": "TaskReadModelEventV4OrV5",
            "type": "object", "additionalProperties": False,
            "properties": {
                "schema_version": {"enum": [4, 5, 6]}, "command_id": uuid,
                "row_group_token": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "event_id": uuid, "operation_id": uuid,
                "event_type": {"enum": sorted(EVENT_TYPES)},
                "task_id": {"type": "string", "maxLength": 96,
                            "pattern": "^(" + "|".join(sorted(SOURCE_TABLE_NAMES)) + "):[1-9][0-9]{0,18}$"},
                "source_id": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1},
                "revision": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
                "changed_fields": {"type": "array", "uniqueItems": True, "maxItems": 64,
                                   "items": {"enum": sorted(CHANGED_FIELDS)}},
                "view_domain": {"enum": sorted({
                    "address_match", "watch", "qmf", "residence", "registration", "review", "task_graph",
                })},
                "task_revision": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1},
                "timestamp": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?Z$"},
                "environment": {"const": environment},
                "run_id": {"type": "string", "pattern": run_pattern},
            }}
    contract["properties"]["encrypted_payload"] = {
            "type": "object", "additionalProperties": False,
            "required": ["algorithm", "key_version", "nonce", "ciphertext", "aad", "ciphertext_sha256"],
            "properties": {
                "algorithm": {"const": "AES-256-GCM"},
                "key_version": {"enum": ["task-event-v2", "task-event-v3", "task-view-state-v1"]},
                "nonce": {"type": "string", "minLength": 16, "maxLength": 16},
                "ciphertext": {"type": "string", "minLength": 1, "maxLength": 350000},
                "aad": {"type": "string", "minLength": 1, "maxLength": 512},
                "ciphertext_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        }
    contract["oneOf"] = [
                {"properties": {"schema_version": {"const": 4}}, "required": list(V4_EVENT_FIELDS)},
        {"properties": {"schema_version": {"const": 5}}, "required": list(V5_REQUIRED_FIELDS)},
                {"properties": {"schema_version": {"const": 6}}, "required": list(V6_REQUIRED_FIELDS)},
    ]
    contract["allOf"] = [{
        "if": {"properties": {"schema_version": {"enum": [4, 6]},
                               "event_type": {"const": "task.deleted"}}},
        "then": {"not": {"required": ["encrypted_payload"]}},
        "else": {"required": ["encrypted_payload"]},
    }]
    contract["allOf"].append({
        "if": {"properties": {"schema_version": {"const": 6},
                               "event_type": {"not": {"const": "task.deleted"}}}},
        "then": {"required": ["row_group_token"]},
    })
    return contract


def summary_schema(environment: str = "development"):
    summary_topic_for(environment)
    run_pattern = (
        "^dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
        if environment == "development" else "^STG-[0-9]{8}-[0-9]{2}$"
    )
    uuid = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
    token = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "OnlineSummaryFactV1",
        "type": "object", "additionalProperties": False,
        "required": [
            "contract_version", "event_type", "event_id", "command_id", "operation_id",
            "environment", "run_id", "task_id", "source_id", "revision", "task_revision",
            "business_date", "parser_type", "row_key_hmac", "community_token", "inspector_token",
            "task_state", "included", "online_present", "unable_to_verify", "reached_bottom",
            "effective_workload", "activity_kind", "correction", "deleted", "occurred_at",
        ],
        "properties": {
            "contract_version": {"const": 1},
            "event_type": {"enum": ["summary.task.changed", "summary.task.deleted"]},
            "event_id": uuid, "command_id": uuid, "operation_id": uuid,
            "environment": {"const": environment}, "run_id": {"type": "string", "pattern": run_pattern},
            "task_id": {"type": "string", "pattern": "^t_[a-z0-9_]+:[1-9][0-9]{0,18}$"},
            "source_id": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1},
            "revision": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1},
            "task_revision": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1},
            "business_date": {"type": "string", "format": "date"},
            "parser_type": {"type": "string", "minLength": 1, "maxLength": 80, "pattern": "^[^\\r\\n]{1,80}$"},
            "row_key_hmac": token, "community_token": token, "inspector_token": token,
            "task_state": {"enum": ["unchecked", "checked", "completed"]},
            "included": {"type": "integer", "enum": [0, 1]},
            "online_present": {"type": "integer", "enum": [0, 1]},
            "unable_to_verify": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
            "reached_bottom": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
            "effective_workload": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
            "activity_kind": {"enum": ["carryover", "new", "changed", "state_transition", "removed"]},
            "correction": {"type": "boolean"}, "deleted": {"type": "boolean"},
            "occurred_at": {"type": "string", "format": "date-time"},
        },
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Schema Registry redirect refused")


def request(path, method="GET", body=None, *, subject=SUBJECT):
    allowed_subjects = {
        subject_for("development"), subject_for("staging"),
        summary_subject_for("development"), summary_subject_for("staging"),
    }
    if subject not in allowed_subjects:
        raise ValueError("unsupported schema subject")
    allowed = {f"/subjects/{subject}/versions", f"/subjects/{subject}/versions/latest", f"/config/{subject}"}
    if path not in allowed:
        raise ValueError("unsupported schema route")
    payload = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=payload, method=method,
                                 headers={"Content-Type": "application/vnd.schemaregistry.v1+json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=10) as response:
        data = response.read(65537)
    if len(data) > 65536:
        raise ValueError("schema response too large")
    return json.loads(data)


def _verify_subject(environment: str, subject: str, expected: dict):
    result = request(f"/subjects/{subject}/versions/latest", subject=subject)
    if result.get("schemaType") != "JSON" or json.loads(result.get("schema", "null")) != expected:
        raise ValueError("non-Production schema identity mismatch")
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"environment": environment, "subject": subject, "schema_id": result["id"],
            "schema_version": result["version"], "contract_sha256": digest, "verified": True}


def verify(environment: str | None = None):
    environment = environment or os.environ.get("APP_ENVIRONMENT", "development")
    task = _verify_subject(environment, subject_for(environment), schema(environment))
    summary = _verify_subject(environment, summary_subject_for(environment), summary_schema(environment))
    return {**task, "environment": environment, "verified": True,
            "subjects": {"task": task, "online_summary": summary}}


def apply(environment: str | None = None):
    environment = environment or os.environ.get("APP_ENVIRONMENT", "development")
    for subject, contract in (
        (subject_for(environment), schema(environment)),
        (summary_subject_for(environment), summary_schema(environment)),
    ):
        request(f"/config/{subject}", "PUT", {"compatibility": "FULL_TRANSITIVE"}, subject=subject)
        request(f"/subjects/{subject}/versions", "POST",
                {"schemaType": "JSON", "schema": json.dumps(contract)}, subject=subject)
    return verify(environment)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("apply", "verify"))
    args = parser.parse_args()
    try:
        print(json.dumps(apply() if args.mode == "apply" else verify()))
    except Exception:
        raise SystemExit("Dev schema registration/verification failed") from None
