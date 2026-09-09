"""Safe, auditable environment provisioning guard.

The script intentionally performs no destructive operation. `preview` emits a
manifest; the server wrapper may consume it after backup and isolation review.
"""
from __future__ import annotations
import argparse, hashlib, json, pathlib, secrets

ROOT = pathlib.Path(__file__).resolve().parent
ENV = {"development": {"prefix": "binhu-development", "cookie": "binhu_dev_session", "data": "synthetic"}, "staging": {"prefix": "binhu-staging", "cookie": "binhu_staging_session", "data": "sanitized-production-copy"}}

def manifest(name: str) -> dict:
    spec = ENV[name]
    return {"environment": name, "compose_project": spec["prefix"], "cookie": spec["cookie"], "data_kind": spec["data"], "network": f"{spec['prefix']}-internal", "volumes": [f"{spec['prefix']}-mysql", f"{spec['prefix']}-redis"], "secret_ids": [hashlib.sha256(secrets.token_bytes(32)).hexdigest()]}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["preview"])
    parser.add_argument("--environment", choices=sorted(ENV), required=True)
    args = parser.parse_args()
    print(json.dumps(manifest(args.environment), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
