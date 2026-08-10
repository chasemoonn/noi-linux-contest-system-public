from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.frontend import CaddyFrontend


class FrontendTests(unittest.TestCase):
    @patch("services.frontend.requests.post")
    def test_caddy_frontend_switches_desktop_upstream(self, post):
        post.return_value.status_code = 200
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            caddyfile = root / "Caddyfile"
            snippet = root / "exam.conf"
            caddyfile.write_text(f"import {snippet}\n", encoding="utf-8")
            frontend = CaddyFrontend(
                {
                    "domain": "exam.example.test",
                    "snippet_path": str(snippet),
                    "caddyfile_path": str(caddyfile),
                    "admin_url": "http://127.0.0.1:2019",
                }
            )

            frontend.enable("203.0.113.20", 80)
            enabled = snippet.read_text(encoding="utf-8")
            self.assertIn("reverse_proxy http://203.0.113.20:80", enabled)
            self.assertIn("header_up Host {upstream_hostport}", enabled)
            self.assertIn(
                "header_down Location ^http://[^/]+ https://{http.request.host}",
                enabled,
            )

            frontend.disable()
            disabled = snippet.read_text(encoding="utf-8")
            self.assertIn('respond "比赛桌面尚未开放" 503', disabled)
            self.assertEqual(post.call_count, 2)
