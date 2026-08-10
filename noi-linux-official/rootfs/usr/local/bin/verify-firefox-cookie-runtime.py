#!/usr/bin/env python3
"""Black-box Firefox cookie-policy verification for image release."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


COOKIE_NAME = "noi_cookie_probe"
COOKIE_VALUE = "first_party_ok"


class ProbeState:
    def __init__(self) -> None:
        self.first_party = threading.Event()
        self.third_party_blocked = threading.Event()
        self.persisted = threading.Event()


class ProbeHandler(BaseHTTPRequestHandler):
    state: ProbeState

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def _cookie_present(self, name: str, value: str) -> bool:
        parts = [part.strip() for part in self.headers.get("Cookie", "").split(";")]
        return f"{name}={value}" in parts

    def _html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path == "/first":
            self.send_response(303)
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={COOKIE_VALUE}; Path=/; Max-Age=3600; "
                "HttpOnly; SameSite=Strict",
            )
            self.send_header("Location", "/first-check")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        elif path == "/first-check":
            if self._cookie_present(COOKIE_NAME, COOKIE_VALUE):
                self.state.first_party.set()
                port = self.server.server_address[1]
                self._html(
                    '<!doctype html><meta http-equiv="refresh" '
                    f'content="0; url=http://127.0.0.2:{port}/third-top">'
                    "first-party cookie accepted"
                )
            else:
                self.send_error(409, "first-party cookie missing")
        elif path == "/third-top":
            port = self.server.server_address[1]
            self._html(
                "<!doctype html><iframe "
                f'src="http://127.0.0.1:{port}/third-set"></iframe>'
            )
        elif path == "/third-set":
            self.send_response(303)
            self.send_header("Set-Cookie", "noi_third_party=blocked; Path=/")
            self.send_header("Location", "/third-check")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        elif path == "/third-check":
            if not self._cookie_present("noi_third_party", "blocked"):
                self.state.third_party_blocked.set()
                self._html("third-party cookie rejected")
            else:
                self.send_error(409, "third-party cookie was accepted")
        elif path == "/persist-check":
            if self._cookie_present(COOKIE_NAME, COOKIE_VALUE):
                self.state.persisted.set()
                self._html("persistent cookie survived restart")
            else:
                self.send_error(409, "persistent cookie missing after restart")
        else:
            self.send_error(404)


def wait_for(event: threading.Event, process: subprocess.Popen, label: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if event.wait(0.1):
            return
        if process.poll() is not None:
            raise RuntimeError(f"Firefox exited before {label}: rc={process.returncode}")
    raise RuntimeError(f"timed out waiting for {label}")


def stop_firefox(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    firefox = shutil.which("firefox")
    if not firefox:
        raise RuntimeError("firefox not found")

    with tempfile.TemporaryDirectory(prefix="noi-firefox-cookie-probe.") as root_name:
        root = Path(root_name)
        home = root / "home"
        profile = home / ".mozilla" / "firefox" / "probe.default"
        profile.mkdir(parents=True)
        # Reproduce the failure mode seen in the official NOI profile. A plain
        # clean profile is not enough: the enterprise policy must override an
        # existing user preference that rejects every cookie.
        (profile / "user.js").write_text(
            'user_pref("network.cookie.cookieBehavior", 2);\n',
            encoding="utf-8",
        )
        (profile.parent / "profiles.ini").write_text(
            "[General]\nStartWithLastProfile=1\nVersion=2\n\n"
            "[Profile0]\nName=probe\nIsRelative=1\nPath=probe.default\nDefault=1\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.update({"HOME": str(home), "MOZ_HEADLESS": "1"})
        state = ProbeState()
        ProbeHandler.state = state
        server = ThreadingHTTPServer(("0.0.0.0", 0), ProbeHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        port = server.server_address[1]
        browser = None
        restarted = None
        try:
            browser = subprocess.Popen(
                [
                    firefox,
                    "--headless",
                    "--profile",
                    str(profile),
                    f"http://127.0.0.1:{port}/first",
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            lock_deadline = time.monotonic() + 15
            while time.monotonic() < lock_deadline:
                if browser.poll() is not None:
                    raise RuntimeError(f"initial Firefox exited: rc={browser.returncode}")
                if (profile / ".parentlock").exists() or (profile / "lock").exists():
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Firefox did not lock the probe profile")

            # One real Firefox process performs the 303 first-party flow and is
            # then redirected to a different top-level loopback host containing
            # a third-party iframe. This avoids Firefox 79 headless remote-window
            # behavior, which differs from a normal GNOME desktop session.
            wait_for(state.first_party, browser, "first-party cookie roundtrip")
            wait_for(state.third_party_blocked, browser, "third-party cookie rejection")

            stop_firefox(browser)
            browser = None
            screenshot = root / "persist.png"
            restarted = subprocess.Popen(
                [
                    firefox,
                    "--headless",
                    "--profile",
                    str(profile),
                    "--screenshot",
                    str(screenshot),
                    f"http://127.0.0.1:{port}/persist-check",
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wait_for(state.persisted, restarted, "cookie persistence after restart")
            restarted.wait(timeout=20)
            if restarted.returncode != 0 or not screenshot.is_file():
                raise RuntimeError("restarted Firefox did not complete the screenshot probe")
        finally:
            if browser is not None:
                stop_firefox(browser)
            if restarted is not None:
                stop_firefox(restarted)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    print("firefox_cookie_runtime_verified")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Firefox cookie runtime verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
