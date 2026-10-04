"""Include executable browser-agent transport tests in the existing CI gate."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class CapacityBrowserTransportTests(unittest.TestCase):
    def test_node_transport_and_real_tls_suite(self):
        node = shutil.which('node')
        openssl = shutil.which('openssl')
        ssh_keygen = shutil.which('ssh-keygen')
        if not node or not openssl or not ssh_keygen:
            if os.name == 'nt':
                self.skipTest('Node, OpenSSL and ssh-keygen are required for transport integration')
            self.fail('Linux qualification requires Node, OpenSSL and ssh-keygen for capacity transport tests')
        tests = sorted((ROOT / 'capacity-browser-agent/test').glob('*.test.mjs'))
        self.assertTrue(tests)
        # The library intentionally rejects world-writable ancestors such as
        # /tmp. Use a disposable directory under this test user's private home.
        with tempfile.TemporaryDirectory(prefix='noi-browser-unit-', dir=Path.home()) as temporary:
            env = dict(os.environ, TMPDIR=temporary, TMP=temporary, TEMP=temporary,
                       NOI_TEST_OPENSSL=openssl)
            result = subprocess.run(
                [node, '--test', '--test-reporter=tap', *map(str, tests)],
                cwd=ROOT, env=env, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=45,
            )
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-8000:])
        self.assertIn('# skipped 0', result.stdout)
