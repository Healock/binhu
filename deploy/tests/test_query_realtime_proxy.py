from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class QueryRealtimeProxyTests(unittest.TestCase):
    def test_audit_stream_is_unbuffered_and_environment_isolated(self):
        for path in ['nginx/binhu.conf', 'nginx/migration/new-app-locations.conf']:
            text = (ROOT / path).read_text(encoding='utf-8')
            for prefix, port in [('/api', 37125), ('/shadow-api', 47125)]:
                with self.subTest(path=path, prefix=prefix):
                    block = text.split(f'location = {prefix}/admin/ops/audit/stream {{', 1)[1].split('}', 1)[0]
                    self.assertIn(f'proxy_pass http://127.0.0.1:{port};', block)
                    self.assertIn('proxy_buffering off;', block)
                    self.assertIn('proxy_cache off;', block)
                    self.assertIn('access_log off;', block)
                    self.assertIn('proxy_read_timeout 75s;', block)

    def test_production_and_shadow_have_dedicated_websocket_routes(self):
        for path in ['nginx/binhu.conf', 'nginx/migration/new-app-locations.conf']:
            text = (ROOT / path).read_text(encoding='utf-8')
            for prefix, port in [('/api/query/live/', 37125), ('/shadow-api/query/live/', 47125)]:
                with self.subTest(path=path, prefix=prefix):
                    block = text.split(f'location ^~ {prefix} {{', 1)[1].split('}', 1)[0]
                    self.assertIn(f'proxy_pass http://127.0.0.1:{port};', block)
                    self.assertIn('proxy_set_header Upgrade $http_upgrade;', block)
                    self.assertIn('proxy_set_header Connection "upgrade";', block)
                    self.assertIn('proxy_buffering off;', block)
                    self.assertIn('access_log off;', block)
