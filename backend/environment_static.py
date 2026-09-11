"""Render a shared static bundle below a fixed environment prefix."""
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

PREFIXES = {'development': '/dev/', 'staging': '/staging/'}


def frontend_index_response(index_path, environment):
    # Lazy import keeps the renderer usable by host-side preparation tools.
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    if environment not in PREFIXES:
        return FileResponse(index_path)
    try:
        html = render_environment_index(Path(index_path).read_text(encoding='utf-8'), environment)
    except (ValueError, OSError):
        return JSONResponse(status_code=503, content={
            'error': 'environment_static_bundle_invalid', 'environment': environment,
        }, headers={'Cache-Control': 'no-store'})
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


class _BundleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.heads = 0
        self.modules = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'head':
            self.heads += 1
        if tag == 'base':
            raise ValueError('environment_static_bundle_invalid')
        for name, value in attrs:
            if name not in ('src', 'href'):
                continue
            url = urlsplit(value or '')
            path = unquote(url.path)
            if (not path or url.scheme or url.netloc or path.startswith('/')
                    or '\\' in path or '..' in path.split('/') or '%' in path):
                raise ValueError('environment_static_bundle_invalid')
        if tag == 'script' and dict(attrs).get('type') == 'module' and dict(attrs).get('src'):
            self.modules += 1


def render_environment_index(html: str, environment: str) -> str:
    """Only the HTML base varies; JS, CSS and immutable on-disk HTML stay identical.

    Relative Vite imports resolve from their module URLs. The fixed base also
    handles public scripts, styles and SPA deep links without a production
    fallback. An old root-based bundle must be rebuilt, never silently served.
    """
    if environment not in PREFIXES:
        raise ValueError('environment_static_identity_invalid')
    parser = _BundleParser()
    parser.feed(html)
    parser.close()
    if parser.heads != 1 or not parser.modules:
        raise ValueError('environment_static_bundle_invalid')
    return re.sub(r'(<head\b[^>]*>)', lambda match: match[0] + f'<base href="{PREFIXES[environment]}">',
                  html, count=1, flags=re.IGNORECASE)
