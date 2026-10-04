"""Actual JS signer -> ASGI HTTP exchange; no cloud or OJ network access."""
import asyncio
from http.cookies import SimpleCookie
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlencode
from services.store import Store

try:
    from test_web_submit import main
except ModuleNotFoundError:
    from tests.test_web_submit import main


class TeacherSsoHttpTests(unittest.TestCase):
    def test_plugin_ticket_cookie_replay_and_contest_scope(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the real plugin signer")
        plugin = Path(__file__).resolve().parents[2] / "hydro-plugin-orchestrator/index.js"
        if not plugin.exists():
            self.skipTest("full source checkout is required for plugin interoperability")
        # Execute the exact checked-in signer, not a Python imitation of it.
        script = r"""
const fs = require('node:fs'), vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(input.plugin, 'utf8');
const start = source.indexOf('function makeTeacherTicket(');
const end = source.indexOf('\nconst NOTIFICATION_ALLOWED_HOSTS', start);
if (start < 0 || end < 0) throw new Error('signer source boundary changed');
const context = {crypto: require('node:crypto'), Buffer, TOKEN: input.key};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
process.stdout.write(context.makeTeacherTicket({uid:42,uname:'测试教练',tid:input.tid}));
"""
        tid = "1" * 24
        result = subprocess.run(
            [node, "-e", script],
            input=json.dumps({"plugin": str(plugin), "key": str(main.cfg["hydro"]["orchestrator_token"]), "tid": tid}),
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, "plugin signer execution failed")
        ticket = result.stdout

        async def exercise():
            # Exercise ASGI HTTP routing without network, cloud lifespan or new dependencies.
            class Client:
                def __init__(self):
                    self.cookies = {}

                async def get(self, path, params=None):
                    messages = []
                    async def receive():
                        return {"type": "http.request", "body": b"", "more_body": False}
                    async def send(message):
                        messages.append(message)
                    headers = [(b"host", b"exam.example.test")]
                    if self.cookies:
                        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()).encode("ascii")))
                    await main.app({"type": "http", "asgi": {"version": "3.0"},
                        "http_version": "1.1", "method": "GET", "scheme": "https",
                        "path": path, "raw_path": path.encode("ascii"), "root_path": "",
                        "query_string": urlencode(params or {}).encode("ascii"),
                        "headers": headers, "client": ("127.0.0.1", 12345),
                        "server": ("exam.example.test", 443)}, receive, send)
                    start = next(m for m in messages if m["type"] == "http.response.start")
                    response = mock.Mock(status_code=start["status"], headers={k.decode(): v.decode() for k, v in start["headers"]})
                    response.text = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body").decode("utf-8")
                    if "set-cookie" in response.headers:
                        parsed = SimpleCookie(response.headers["set-cookie"])
                        self.cookies.update({k: v.value for k, v in parsed.items()})
                    return response

            client = Client()
            response = await client.get("/admin/sso", params={"ticket": ticket})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/admin")
            cookie = response.headers["set-cookie"]
            for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
                self.assertIn(attribute, cookie)
            self.assertNotIn("Domain=", cookie)
            self.assertIsNotNone(client.cookies.get(main.TEACHER_COOKIE))
            # Render the real templates against a real, disposable SQLite store.
            with mock.patch.object(main, "_admin_page", wraps=main._admin_page) as page:
                for _ in range(2):
                    rendered = await client.get("/admin")
                    self.assertEqual(rendered.status_code, 200)
                    identity = page.call_args.args[1]
                    self.assertEqual((identity.uid, identity.uname, identity.tid), (42, "测试教练", tid))
                for section, label in main.ADMIN_SECTIONS.items():
                    rendered = await client.get("/admin/" + section)
                    self.assertEqual(rendered.status_code, 200)
                    self.assertIn(label, rendered.text)
                    self.assertIn("NOI_RENDER_OWN", rendered.text)
                    self.assertNotIn("NOI_RENDER_OTHER", rendered.text)
                    self.assertEqual(rendered.headers.get("cache-control"), "no-store")
            replay = await client.get("/admin/sso", params={"ticket": ticket})
            self.assertEqual(replay.status_code, 409)
            self.assertNotIn("set-cookie", replay.headers)
            with mock.patch.object(main.store, "get_contest") as lookup:
                denied = await client.get("/admin/export/" + "2" * 24)
                self.assertEqual(denied.status_code, 403)
                lookup.assert_not_called()
            client.cookies.clear()
            self.assertEqual((await client.get("/admin")).status_code, 401)
            malformed = await client.get("/admin/sso", params={"ticket": "abcdefghijklmnop.中文"})
            self.assertEqual(malformed.status_code, 401)

        with tempfile.TemporaryDirectory(prefix="noi-teacher-http-") as directory:
            store = Store(str(Path(directory) / "test.db"))
            try:
                store.upsert_contest(tid, "NOI_RENDER_OWN", ["apple"], {"apple": "P1"})
                store.upsert_contest("2" * 24, "NOI_RENDER_OTHER", ["apple"], {"apple": "P1"})
                with mock.patch.object(main, "_USED_TEACHER_TICKETS", {}), mock.patch.object(main, "store", store), mock.patch.object(main.hydro, "get_contest", side_effect=AssertionError("unexpected external OJ request")):
                    asyncio.run(exercise())
            finally:
                store.close()
