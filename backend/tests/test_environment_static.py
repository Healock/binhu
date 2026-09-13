"""The same frontend bundle must stay inside either nonproduction entry."""
import unittest
import tempfile
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin

from environment_static import render_environment_index, frontend_index_response


INDEX = '''<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="./startup-guard.css">
<script src="./startup-guard.js"></script>
<script type="module" src="./assets/app-123.js"></script>
<link rel="modulepreload" href="./assets/vendor-456.js">
<link rel="stylesheet" href="./assets/app-123.css">
</head><body><div id="root"></div></body></html>'''


class Resources(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.base = None
        self.urls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'base':
            self.base = attrs['href']
        else:
            self.urls.extend(v for k, v in attrs.items() if k in ('src', 'href'))


class EnvironmentStaticTests(unittest.TestCase):
    def test_http_response_uses_configured_environment_and_never_caches_index(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / 'index.html'
            index.write_text(INDEX, encoding='utf-8')
            app = FastAPI()

            @app.get('/{path:path}')
            def page(path: str):
                return frontend_index_response(index, 'development')

            client = TestClient(app)
            for path in ['/login', '/tasks/detail/123', '/index.html']:
                response = client.get(path, headers={'X-Forwarded-Prefix': '/staging/'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get('cache-control'), 'no-store')
                self.assertIn('<base href="/dev/">', response.text)
            self.assertEqual(index.read_text(encoding='utf-8'), INDEX)

    def test_invalid_bundle_returns_structured_503_without_file_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / 'index.html'
            index.write_text(INDEX.replace('./assets/app-123.js', '/assets/app-123.js'))
            response = frontend_index_response(index, 'staging')
            self.assertEqual(response.status_code, 503)
            self.assertIn(b'environment_static_bundle_invalid', response.body)
            self.assertNotIn(b'app-123', response.body)
            self.assertNotIn(str(index).encode(), response.body)

    def test_production_index_remains_unchanged(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / 'index.html'
            html = INDEX.replace('./assets/', '/assets/')
            index.write_text(html, encoding='utf-8', newline='\n')
            app = FastAPI()

            @app.get('/')
            def page():
                return frontend_index_response(index, 'production')

            self.assertEqual(TestClient(app).get('/').text, html)

    def test_same_bundle_resolves_inside_environment_even_on_nested_routes(self):
        for environment, prefix in [('development', '/dev/'), ('staging', '/staging/')]:
            html = render_environment_index(INDEX, environment)
            resources = Resources(html)
            self.assertEqual(resources.base, prefix)
            self.assertLess(html.index('<base '), html.index('startup-guard.css'))
            for route in ['', 'login', 'tasks/detail/123']:
                base = urljoin('https://example.test' + prefix + route, resources.base)
                for resource in resources.urls:
                    with self.subTest(environment=environment, route=route, resource=resource):
                        self.assertTrue(urljoin(base, resource).startswith('https://example.test' + prefix))
            self.assertEqual(html.replace(f'<base href="{prefix}">', ''), INDEX)

    def test_rejects_old_root_bundle_or_untrusted_resource_paths(self):
        for url in ['/assets/app.js', '//example.test/app.js', 'https://example.test/app.js',
                    '../assets/app.js', './x/../../assets/app.js', './%2e%2e/assets/app.js',
                    '.\\assets\\app.js', 'data:text/javascript,alert(1)']:
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'environment_static_bundle_invalid'):
                render_environment_index(INDEX.replace('./assets/app-123.js', url), 'development')

    def test_rejects_missing_head_or_existing_base(self):
        for html in [INDEX.replace('<head>', ''), INDEX.replace('<head>', '<head><base href="/">'),
                     INDEX.replace('<head>', '<head><head>')]:
            with self.subTest(html=html), self.assertRaises(ValueError):
                render_environment_index(html, 'development')

    def test_unknown_environment_cannot_silently_choose_production(self):
        for environment in ['production', 'shadow', '', 'Development']:
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                render_environment_index(INDEX, environment)


if __name__ == '__main__':
    unittest.main()
