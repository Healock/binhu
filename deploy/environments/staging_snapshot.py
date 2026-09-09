"""Guarded staging snapshot pipeline.

The exporter accepts an already-created, allow-listed JSONL snapshot.  It never
connects to production itself; production extraction is performed by the
server's reviewed read-only job.  This boundary makes accidental raw dumps
impossible for the import tool.
"""
from __future__ import annotations

import argparse, hashlib, json, re, secrets
from pathlib import Path

SENSITIVE = re.compile(r"姓名|身份证|手机号|密码|token|cookie|照片|附件|原始地址|核查补充|核查反馈|备注|source_text|person[_-]?name|phone|identity[_-]?number", re.I)
REQUIRED = {"record_type", "record_key", "community_key", "address_key", "value"}

def snapshot_id() -> str:
    return "staging-" + secrets.token_hex(8)

def sanitize(src: Path, dst: Path) -> dict:
    counts = {"input": 0, "output": 0, "rejected": 0}
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open(encoding="utf-8") as r, dst.open("x", encoding="utf-8") as w:
        for line in r:
            counts["input"] += 1
            item = json.loads(line)
            if not REQUIRED <= item.keys():
                counts["rejected"] += 1
                continue
            # Preserve relationship keys, replace the payload with a stable
            # per-snapshot token; production values never enter staging.
            token = hashlib.sha256((str(item["record_key"]) + str(item["value"])).encode()).hexdigest()[:16]
            item["value"] = f"synthetic-{token}"
            for key in list(item):
                if SENSITIVE.search(str(key)):
                    item[key] = f"synthetic-{hashlib.sha256((str(key)+str(item[key])).encode()).hexdigest()[:16]}"
            w.write(json.dumps(item, ensure_ascii=False) + "\n")
            counts["output"] += 1
    if counts["rejected"]:
        raise SystemExit("sanitization rejected records; review the private report before import")
    return counts

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["id", "sanitize", "verify"])
    p.add_argument("--source", type=Path)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    if args.action == "id":
        print(snapshot_id()); return
    if not args.source or not args.output:
        p.error("--source and --output are required")
    if args.action == "sanitize":
        print(json.dumps(sanitize(args.source, args.output)))
    else:
        rows = [json.loads(x) for x in args.output.read_text(encoding="utf-8").splitlines() if x]
        leaked = [x for x in rows if SENSITIVE.search(json.dumps(x, ensure_ascii=False))]
        if leaked: raise SystemExit("sensitive value detected")
        print(json.dumps({"verified": True, "rows": len(rows)}))

if __name__ == "__main__":
    main()
