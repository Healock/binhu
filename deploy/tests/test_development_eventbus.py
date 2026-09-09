import tempfile, unittest, json
from pathlib import Path
from deploy.environments.development_eventbus import prepare

class DevEventbusTests(unittest.TestCase):
    def test_prepare_isolates_state_and_redacts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); src=root/'shadow'; src.mkdir(); (src/'docker-compose.yml').write_text('name: shadow\nservices: {}\n'); (src/'secret.env').write_text('PASSWORD=x')
            out=root/'dev'; m=prepare(src,out,'dev-test-1')
            self.assertFalse(m['state_copied']); self.assertFalse(m['volumes_copied']); self.assertEqual(m['topic_namespace'],'dev.')
            self.assertIn('development', (out/'docker-compose.yml').read_text())

if __name__ == '__main__': unittest.main()
