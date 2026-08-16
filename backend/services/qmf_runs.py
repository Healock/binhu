"""Persistent, privacy-safe state for single-item 全民防 registrations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


PREPARE_TTL_SECONDS = 600
TENCENT_MARKER = "滨湖平台已完成全民防反馈"

RUN_STEPS: tuple[tuple[str, str], ...] = (
    ("query_task", "查询模型三任务"),
    ("query_person", "查询人员登记资料"),
    ("query_photo", "读取居住证照片"),
    ("precheck", "执行登记前校验"),
    ("upload_photo", "上传照片数据"),
    ("save_local_photo", "保存居住证照片关联"),
    ("register_person", "保存人员登记"),
    ("complete_task", "反馈模型三核查结果"),
    ("verify_final", "复核模型三最终状态"),
)


def initial_steps() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": label,
            "status": "pending",
            "result_code": "",
            "started_at": None,
            "finished_at": None,
        }
        for key, label in RUN_STEPS
    ]


def serialize_steps(steps: list[dict[str, Any]]) -> str:
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))


def parse_steps(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        raw = value
    else:
        try:
            raw = json.loads(str(value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = []
    known = {key: label for key, label in RUN_STEPS}
    result: list[dict[str, Any]] = []
    by_key = {
        str(item.get("key") or ""): item
        for item in raw
        if isinstance(item, dict)
    }
    for key, label in RUN_STEPS:
        item = by_key.get(key) or {}
        result.append({
            "key": key,
            "label": label,
            "status": str(item.get("status") or "pending"),
            "result_code": str(item.get("result_code") or "")[:64],
            "started_at": item.get("started_at"),
            "finished_at": item.get("finished_at"),
        })
    return result


def utc_text(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


async def ensure_qmf_registration_schema(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _qmf_registration_runs (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            parser_type VARCHAR(100) NOT NULL,
            row_key_digest CHAR(64) NOT NULL,
            source_id BIGINT NOT NULL,
            expected_revision INT NOT NULL,
            expected_row_hash CHAR(64) NOT NULL,
            idempotency_key CHAR(64) NOT NULL,
            requested_by INT NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'prepared',
            steps_json JSON NOT NULL,
            result_code VARCHAR(64) NOT NULL DEFAULT '',
            upstream_task_digest CHAR(64) NOT NULL DEFAULT '',
            photo_sha256 CHAR(64) NOT NULL DEFAULT '',
            photo_mime_type VARCHAR(50) NOT NULL DEFAULT '',
            photo_size_bytes INT NOT NULL DEFAULT 0,
            tencent_marker_status VARCHAR(32) NOT NULL DEFAULT 'not_started',
            tencent_marker_error VARCHAR(64) NOT NULL DEFAULT '',
            prepared_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NOT NULL,
            execution_started_at DATETIME DEFAULT NULL,
            completed_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_qmf_run_task (parser_type, source_id, id),
            INDEX idx_qmf_run_status (status, updated_at),
            INDEX idx_qmf_run_idempotency (idempotency_key, status),
            INDEX idx_qmf_run_upstream (upstream_task_digest, status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    # Executions are intentionally not resumable.  If the process stopped while
    # a request was in flight, the external result cannot be inferred safely.
    # Persist the interrupted step as uncertain as well, otherwise the page
    # would misleadingly keep showing a permanently running request.
    await cur.execute(
        "SELECT id, steps_json FROM _qmf_registration_runs WHERE status='executing'"
    )
    interrupted = await cur.fetchall()
    interrupted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for run_id, raw_steps in interrupted:
        steps = parse_steps(raw_steps)
        for item in steps:
            if item["status"] == "sending":
                item["status"] = "uncertain"
                item["result_code"] = "process_interrupted"
                item["finished_at"] = interrupted_at
        await cur.execute(
            "UPDATE _qmf_registration_runs "
            "SET status='uncertain', result_code='process_interrupted', steps_json=%s "
            "WHERE id=%s AND status='executing'",
            (serialize_steps(steps), run_id),
        )
    await cur.execute("""
        UPDATE _qmf_registration_runs
        SET tencent_marker_status='pending',
            tencent_marker_error='process_interrupted'
        WHERE tencent_marker_status='writing'
    """)
