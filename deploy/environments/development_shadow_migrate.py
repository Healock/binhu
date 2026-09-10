"""Capture hashes of shadow configuration; never copy arbitrary secret values."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

ALLOWED = {'docker-compose.yml', 'docker-compose.business.yml',
           'docker-compose.derived.yml', 'shadow-gateway.conf'}


def checked_path(path: Path) -> Path:
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError('absolute non-root path required')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlinks forbidden')
    return path.resolve()


def extract(source: Path, output: Path) -> None:
    source, output = checked_path(source), checked_path(output)
    if not source.is_dir() or output.exists() or output.is_relative_to(source):
        raise ValueError('new evidence directory outside source required')
    files = {}
    for name in sorted(ALLOWED):
        path = source / name
        if path.is_symlink():
            raise ValueError('symlink configuration rejected')
        if path.is_file():
            raw = path.read_bytes()
            files[name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    if 'docker-compose.yml' not in files:
        raise ValueError('shadow Compose required')
    output.mkdir(parents=True, mode=0o700)
    target = output / 'manifest.json'
    target.write_text(json.dumps({'template': 'shadow', 'state_copied': False,
        'evidence_only': True, 'artifacts_excluded': True,
        'configuration_values_copied': False, 'files': files}, indent=2), encoding='utf-8')
    target.chmod(0o600)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('source', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    try:
        extract(a.source, a.output)
    except (ValueError, OSError):
        raise SystemExit('evidence capture failed; originals retained') from None


if __name__ == '__main__':
    main()
