"""Generate an isolated Dev event-bus topology from a reviewed shadow template.

The command only copies topology (never state). Secrets and connection strings
are intentionally not accepted from the template; callers provide immutable
image ids and a fresh run id.
"""
from __future__ import annotations
import argparse, hashlib, json, re
from pathlib import Path

SECRET = re.compile(r"password|secret|token|credential|private|connection", re.I)

def _safe_root(path: Path) -> Path:
    path = path.resolve()
    if path.is_symlink() or path == Path('/'):
        raise ValueError('unsafe path')
    return path

def prepare(template: Path, output: Path, run_id: str) -> dict:
    template, output = _safe_root(template), _safe_root(output)
    if not template.is_dir() or output.exists() or not re.fullmatch(r"dev-[A-Za-z0-9._-]+", run_id):
        raise ValueError('invalid template, output, or run id')
    output.mkdir(parents=True, mode=0o700)
    copied = []
    for src in template.rglob('*'):
        if not src.is_file() or 'artifact' in src.parts:
            continue
        rel = src.relative_to(template)
        if rel.suffix not in {'.yml', '.yaml', '.conf', '.toml'}:
            continue
        if SECRET.search(rel.name):
            continue
        text = src.read_text(errors='replace')
        text = re.sub(r'(?im)^([^#\n]*(?:password|secret|token|credential|key|url)[^=\n]*)=.*$', r'\1=[REDACTED]', text)
        text = text.replace('shadow', 'development').replace('SHADOW', 'DEVELOPMENT')
        text = re.sub(r'(?m)^\s*container_name:.*$', '', text)
        dest_rel = Path('compose.yml') if rel.name in {'docker-compose.yml','docker-compose.yaml'} else rel
        dest = output / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding='utf-8'); dest.chmod(0o600)
        copied.append(str(dest_rel))
    compose = output / 'compose.yml'
    if not compose.exists():
        raise ValueError('template compose missing')
    manifest = {'environment':'development','project':'binhu-development-eventbus',
                'run_id':run_id,'state_copied':False,'volumes_copied':False,
                'topic_namespace':'dev.','files':{p: hashlib.sha256((output/p).read_bytes()).hexdigest() for p in copied}}
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8'); (output/'manifest.json').chmod(0o600)
    return manifest

def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument('template', type=Path); p.add_argument('output', type=Path); p.add_argument('--run-id', required=True)
    a=p.parse_args()
    try: print(json.dumps(prepare(a.template,a.output,a.run_id)))
    except (ValueError,OSError) as exc: raise SystemExit(str(exc))

if __name__ == '__main__': main()
