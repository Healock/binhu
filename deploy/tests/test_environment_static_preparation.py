import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.environments import runtime


class EnvironmentStaticPreparationTests(unittest.TestCase):
    def prepare(self, root, html):
        source = root / 'source'
        (source / 'backend').mkdir(parents=True)
        (source / 'VERSION').write_text('0.28.15')
        (source / 'backend/init.sql').write_text('CREATE DATABASE OnlineData;')
        shutil.copyfile(Path(__file__).parents[2] / 'backend/environment_static.py',
                        source / 'backend/environment_static.py')
        static = root / 'static'
        static.mkdir()
        (static / 'index.html').write_text(html)
        return SimpleNamespace(environment='development', source=source, static=static,
                               backend_image='sha256:' + '1' * 64,
                               mysql_image='sha256:' + '2' * 64,
                               redis_image='sha256:' + '3' * 64)

    def test_old_bundle_rejected_before_any_environment_files_are_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.prepare(root, '<html><head><script type="module" src="/assets/app.js"></script></head></html>')
            target = root / 'development'
            with patch.object(runtime, 'root_for', return_value=target), \
                 patch.object(runtime, 'image_id', side_effect=lambda value: value), \
                 patch.object(runtime, 'command') as command, \
                 self.assertRaisesRegex(ValueError, 'environment_static_bundle_invalid'):
                runtime.prepare(args)
            self.assertFalse(target.exists())
            command.assert_not_called()

    def test_preparation_keeps_shared_bundle_bytes_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html = '<html><head><script type="module" src="./assets/app.js"></script></head></html>'
            args = self.prepare(root, html)
            target = root / 'development'
            with patch.object(runtime, 'root_for', return_value=target), \
                 patch.object(runtime, 'image_id', side_effect=lambda value: value), \
                 patch.object(runtime, 'command', return_value=''), \
                 contextlib.redirect_stdout(io.StringIO()):
                runtime.prepare(args)
            self.assertEqual((target / 'static/index.html').read_bytes(), (args.static / 'index.html').read_bytes())
            manifest = json.loads((target / 'manifest.json').read_text())
            self.assertEqual(manifest['environment'], 'development')
            self.assertEqual(manifest['version'], '0.28.15')


if __name__ == '__main__':
    unittest.main()
