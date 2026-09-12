"""Verify ownership of the exclusive Dev Flink checkpoint volume."""
from __future__ import annotations
import argparse
import json
import time
from .prepare import checked, ROOT

VOLUME = "binhu-development_flink-checkpoints"
JM = "binhu-development-flink-jobmanager-1"
TM = "binhu-development-flink-taskmanager-1"
TARGET = "/opt/flink/checkpoints"


def inspect_holders(items):
    holders = []
    for item in items:
        for mount in item.get("Mounts", []):
            if mount.get("Name") != VOLUME:
                continue
            labels = item.get("Config", {}).get("Labels", {})
            if (item["Name"].lstrip("/") not in {JM, TM}
                or labels.get("com.docker.compose.project") != "binhu-development-flink"
                or labels.get("binhu.environment") != "development"
                or mount.get("Destination") != TARGET or not mount.get("RW")
                or set(item["NetworkSettings"]["Networks"]) != {"binhu-development-eventbus_internal"}):
                raise ValueError("checkpoint volume has foreign dependency")
            holders.append(item["Name"].lstrip("/"))
    if sorted(holders) != sorted([JM, TM]):
        raise ValueError("checkpoint volume holders differ")
    return holders


def measure():
    ids = checked(["docker", "ps", "-aq"]).split()
    if not ids:
        raise ValueError("Dev Flink not initialized")
    holders = inspect_holders(json.loads(checked(["docker", "inspect", *ids])))
    uid = checked(["docker", "exec", JM, "id", "-u", "flink"]).strip()
    gid = checked(["docker", "exec", JM, "id", "-g", "flink"]).strip()
    if not uid.isdigit() or not gid.isdigit() or uid == "0":
        raise ValueError("non-root Flink service identity required")
    owner = checked(["docker", "exec", JM, "stat", "-c", "%u:%g", TARGET]).strip()
    return {"environment": "development", "volume": VOLUME, "holders": holders,
            "owner": owner, "expected_owner": uid + ":" + gid,
            "ownership_verified": owner == uid + ":" + gid}


def apply():
    report = measure()
    if ROOT.is_symlink() or ROOT.resolve() != ROOT:
        raise ValueError("private Dev evidence directory required")
    evidence = ROOT / ("checkpoint-owner-" + str(time.time_ns()) + ".json")
    with evidence.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps({"before": report, "completed": False}))
    evidence.chmod(0o600)
    # Only the mounted root directory. Existing checkpoints are never traversed.
    if not report["ownership_verified"]:
        checked(["docker", "exec", "-u", "0", JM, "chown", report["expected_owner"], TARGET])
    after = measure()
    with (ROOT / (evidence.stem + "-after.json")).open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(after))
    return after


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("measure", "apply"))
    args = parser.parse_args()
    try:
        print(json.dumps(apply() if args.mode == "apply" else measure()))
    except Exception:
        raise SystemExit("Dev checkpoint ownership check failed") from None
