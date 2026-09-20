"""Fixed internal Dev Schema Registry contract; no caller-supplied URLs."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import urllib.request

from .services.kafka_event_contract import EVENT_FIELDS, EVENT_TYPES, CHANGED_FIELDS, SOURCE_TABLE_NAMES
from .identity import topic_for

# The deployed registry is Apicurio 2.x. Its Confluent-compatible API is
# explicitly namespaced; 8081 belongs to neither this service nor this API.
BASE = "http://schema-registry:8080/apis/ccompat/v7"
SUBJECT = "dev.task.events.v1-value"


def subject_for(environment: str) -> str:
    return topic_for(environment) + "-value"


def schema(environment: str = "development"):
    topic_for(environment)
    run_pattern = (
        "^dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
        if environment == "development" else "^STG-[0-9]{8}-[0-9]{2}$"
    )
    uuid = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
    return {"$schema": "http://json-schema.org/draft-07/schema#", "title": "DevTaskMetadataV1",
            "type": "object", "additionalProperties": False, "required": list(EVENT_FIELDS),
            "properties": {
                "schema_version": {"const": 1}, "event_id": uuid, "operation_id": uuid,
                "event_type": {"enum": sorted(EVENT_TYPES)},
                "task_id": {"type": "string", "maxLength": 96,
                            "pattern": "^(" + "|".join(sorted(SOURCE_TABLE_NAMES)) + "):[1-9][0-9]{0,18}$"},
                "source_id": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1},
                "revision": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
                "changed_fields": {"type": "array", "uniqueItems": True, "maxItems": 64,
                                   "items": {"enum": sorted(CHANGED_FIELDS)}},
                "timestamp": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?Z$"},
                "environment": {"const": environment},
                "run_id": {"type": "string", "pattern": run_pattern},
            }}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Schema Registry redirect refused")


def request(path, method="GET", body=None, *, subject=SUBJECT):
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


def verify(environment: str | None = None):
    environment = environment or os.environ.get("APP_ENVIRONMENT", "development")
    subject = subject_for(environment)
    expected = schema(environment)
    result = request(f"/subjects/{subject}/versions/latest", subject=subject)
    if result.get("schemaType") != "JSON" or json.loads(result.get("schema", "null")) != expected:
        raise ValueError("non-Production schema identity mismatch")
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"environment": environment, "subject": subject, "schema_id": result["id"],
            "schema_version": result["version"], "contract_sha256": digest, "verified": True}


def apply(environment: str | None = None):
    environment = environment or os.environ.get("APP_ENVIRONMENT", "development")
    subject = subject_for(environment)
    request(f"/config/{subject}", "PUT", {"compatibility": "FULL_TRANSITIVE"}, subject=subject)
    request(f"/subjects/{subject}/versions", "POST",
            {"schemaType": "JSON", "schema": json.dumps(schema(environment))}, subject=subject)
    return verify(environment)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("apply", "verify"))
    args = parser.parse_args()
    try:
        print(json.dumps(apply() if args.mode == "apply" else verify()))
    except Exception:
        raise SystemExit("Dev schema registration/verification failed") from None
