"""Copy the old shadow topology as a Dev template without copying state."""
from __future__ import annotations
import argparse, hashlib, json, re
from pathlib import Path

SECRET = re.compile(r"password|secret|token|credential|private", re.I)

def extract(source: Path, output: Path) -> None:
    if source.resolve() == output.resolve() or not source.exists():
        raise SystemExit("invalid shadow source/output")
    output.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for path in source.rglob("*"):
        if not path.is_file() or SECRET.search(path.name):
            continue
        rel = path.relative_to(source)
        if rel.suffix not in {".yml", ".yaml", ".json", ".env", ".toml", ".conf"}:
            continue
        text = path.read_text(errors="replace")
        text = re.sub(r"(?im)^([^#\n]*(?:password|secret|token|credential)[^=\n]*)=.*$", r"\1=[REDACTED]", text)
        target = output / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        hashes[str(rel)] = hashlib.sha256(text.encode()).hexdigest()
    (output / "manifest.json").write_text(json.dumps({"template": "shadow", "state_copied": False, "hashes": hashes}, indent=2), encoding="utf-8")

def main() -> None:
    p = argparse.ArgumentParser(); p.add_argument("source", type=Path); p.add_argument("output", type=Path); args = p.parse_args(); extract(args.source, args.output)

if __name__ == "__main__": main()
