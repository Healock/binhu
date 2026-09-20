"""Fail-closed Staging load-run contract.

The command is intentionally an orchestration shell, not a deployment bypass:
real prepare/measure/apply/run actions are implemented by the Staging gateway
and must receive this validated run manifest plus an immutable image digest.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

try:
    from .acceptance import assemble_report
    from .guard import require_staging_context
except ImportError:  # direct invocation by the fixed gateway
    from acceptance import assemble_report
    from guard import require_staging_context


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SNAPSHOT_RE = re.compile(r"^staging-[0-9a-f]{16}$")
DEV_RUN_RE = re.compile(r"^dev-[0-9]{8}-[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
PIPELINE_IMAGE_KEYS = frozenset({"kafka", "schema_registry", "mysql", "redis", "worker", "flink"})
ACTIONS = ("prepare", "measure", "apply", "run", "sample", "verify", "rollback")


def _digest(value: object, label: str) -> str:
    result = str(value or "")
    if not DIGEST_RE.fullmatch(result):
        raise RuntimeError(f"{label} 必须使用不可变 sha256 digest")
    return result


def load_manifest(path: str, run_id: str) -> dict:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("run_id") != run_id:
        raise RuntimeError("manifest 运行编号不一致")
    if manifest.get("environment") != "staging":
        raise RuntimeError("manifest 环境必须为 staging")
    digest = _digest(manifest.get("candidate_image_digest"), "候选应用制品")
    if manifest.get("fictional_only") is not True or manifest.get("production_data") is not False:
        raise RuntimeError("Staging manifest 必须声明脱敏/虚构数据且禁止生产数据")
    if not COMMIT_RE.fullmatch(str(manifest.get("commit") or "")):
        raise RuntimeError("manifest 必须绑定完整 Git commit")
    if not SHA256_RE.fullmatch(str(manifest.get("configuration_sha256") or "")):
        raise RuntimeError("manifest 必须绑定配置摘要")
    if not SNAPSHOT_RE.fullmatch(str(manifest.get("staging_snapshot_id") or "")):
        raise RuntimeError("manifest 必须绑定新的 Staging 脱敏快照")

    dev = manifest.get("dev_acceptance")
    if not isinstance(dev, dict) or dev.get("status") != "passed":
        raise RuntimeError("同一应用制品必须先通过 Dev 验收")
    if not DEV_RUN_RE.fullmatch(str(dev.get("run_id") or "")):
        raise RuntimeError("Dev 验收运行编号无效")
    if _digest(dev.get("candidate_image_digest"), "Dev 已验收应用制品") != digest:
        raise RuntimeError("Staging 与 Dev 应用制品 digest 不一致")

    images = manifest.get("event_pipeline_image_digests")
    if not isinstance(images, dict) or set(images) != PIPELINE_IMAGE_KEYS:
        raise RuntimeError("manifest 必须绑定六个 Dev 已验收事件管道镜像")
    for name in sorted(PIPELINE_IMAGE_KEYS):
        _digest(images.get(name), f"事件管道镜像 {name}")
    accepted_images = dev.get("event_pipeline_image_digests")
    if accepted_images != images:
        raise RuntimeError("Staging 与 Dev 事件管道制品 digest 不一致")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--load-report")
    parser.add_argument("--resource-samples")
    parser.add_argument("--rollback-report")
    parser.add_argument("--manual-report")
    args = parser.parse_args()
    context = require_staging_context(args.run_id)
    manifest = load_manifest(args.manifest, context.run_id)
    # Do not execute remote commands here. The fixed Staging gateway consumes
    # this bounded contract in the approved environment.
    result = {
        "acceptance": "contract_validated",
        "action": args.action,
        "run_id": context.run_id,
        "project": context.project,
        "candidate_image_digest": manifest["candidate_image_digest"],
        "commit": manifest["commit"],
        "configuration_sha256": manifest["configuration_sha256"],
        "staging_snapshot_id": manifest["staging_snapshot_id"],
        "dev_acceptance_run_id": manifest["dev_acceptance"]["run_id"],
        "event_pipeline_image_digests": manifest["event_pipeline_image_digests"],
        "production_access": False,
        "staging_database": context.db_name,
    }
    if args.action == "verify":
        evidence = (args.load_report, args.resource_samples, args.rollback_report, args.manual_report)
        if not all(evidence):
            raise RuntimeError("verify 必须提供请求、资源、回滚和页面验收证据")
        result["report"] = assemble_report(
            run_id=context.run_id,
            load_report_path=args.load_report,
            resource_samples_path=args.resource_samples,
            rollback_report_path=args.rollback_report,
            manual_report_path=args.manual_report,
        )
        result["acceptance"] = "passed" if result["report"]["passed"] else "failed"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
