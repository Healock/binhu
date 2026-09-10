"""Inventory a Dev migration proposal; never execute a renamed shadow Compose.

The old implementation rewrote arbitrary YAML and could keep old bind mounts,
external volumes or secrets. This command now emits a non-executable proposal.
A reviewed fresh runtime must be prepared separately from explicit image IDs.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path
try:
    from .development_shadow_migrate import checked_path, extract
except ImportError:
    from development_shadow_migrate import checked_path, extract


def prepare(template: Path, output: Path, run_id: str) -> dict:
    template, output = checked_path(template), checked_path(output)
    if not re.fullmatch(r'dev-[A-Za-z0-9._-]{1,64}', run_id):
        raise ValueError('fresh Dev run ID required')
    extract(template, output)
    evidence = output / 'manifest.json'
    manifest = json.loads(evidence.read_text(encoding='utf-8'))
    manifest.update({'environment': 'development',
        'proposed_project': 'binhu-development-eventbus', 'run_id': run_id,
        'volumes_copied': False, 'topic_namespace': 'dev.',
        'runtime_generated': False, 'started': False, 'acceptance': 'pending'})
    evidence.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('template', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--run-id', required=True)
    a = p.parse_args()
    try:
        print(json.dumps(prepare(a.template, a.output, a.run_id)))
    except (ValueError, OSError):
        raise SystemExit('Dev proposal failed; no runtime started') from None


if __name__ == '__main__':
    main()
