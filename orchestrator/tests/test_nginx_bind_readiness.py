"""Run the cold-boot listener wait script with isolated command fixtures."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'posix', 'Linux shell readiness fixture')
class NginxBindReadinessTests(unittest.TestCase):
    def run_listener_check(self, listeners):
        source = Path(__file__).resolve().parents[2]/'deploy/noi-wait-nginx-bind-addresses'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root/'noi-seats.conf'
            config.write_text('\n'.join(f'    listen {listener};' for listener in listeners))
            script = root/'wait.sh'
            script.write_text(source.read_text().replace('/etc/nginx/conf.d/noi-*.conf', shlex.quote(str(config))))
            binary = root/'bin'
            binary.mkdir()
            (binary/'ip').write_text('#!/bin/sh\nprintf "%s\\n" "1: lo inet 127.0.0.1/8" "2: test inet 192.0.2.2/24"\n')
            (binary/'sleep').write_text('#!/bin/sh\nexit 0\n')
            for path in binary.iterdir():
                path.chmod(0o700)
            return subprocess.run(['sh',str(script)], capture_output=True, timeout=10,
                                  env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH']})

    def test_wildcard_tls_listener_does_not_wait_for_a_nonexistent_interface(self):
        result = self.run_listener_check(['0.0.0.0:443 ssl default_server','127.0.0.1:80 default_server','192.0.2.2:18082'])
        self.assertEqual(result.returncode,0,result.stderr)

    def test_old_wildcard_http_listener_is_still_supported(self):
        result = self.run_listener_check(['0.0.0.0:80 default_server'])
        self.assertEqual(result.returncode,0,result.stderr)

    def test_missing_specific_bridge_address_still_blocks_startup(self):
        result = self.run_listener_check(['0.0.0.0:443 ssl','192.0.2.3:18082'])
        self.assertEqual(result.returncode,1)
        self.assertIn(b'nginx bind address did not appear',result.stderr)
