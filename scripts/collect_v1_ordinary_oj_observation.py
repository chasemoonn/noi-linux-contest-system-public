#!/usr/bin/env python3
"""Collect one privacy-safe ordinary-OJ health and PM2 identity snapshot."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from verify_v1_single_seat_evidence import EvidenceError, validate_ordinary_oj


PROCESS_NAMES = ("caddy", "hydro-sandbox", "hydrooj", "mongodb")
HTTPS_ORIGIN = re.compile(r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]+)?$")


class ObservationError(RuntimeError):
    pass


def require_private_output_parent(path: Path) -> Path:
    parent = Path(os.path.abspath(path)).parent
    if not parent.is_dir() or parent.is_symlink():
        raise ObservationError("observation output parent must be a real directory")
    resolved = parent.resolve()
    if resolved != parent:
        raise ObservationError("observation output parent must be canonical")
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        current = current / part
        info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ObservationError("observation output parent has an unsafe ancestor")
    if stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise ObservationError("observation output parent must be mode 0700 or stricter")
    return parent


def request(origin: str, path: str, *, expect_json: bool = False) -> tuple[int, dict | None]:
    url = origin + path
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    raw_request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json" if expect_json else "text/html"},
    )
    try:
        response = opener.open(raw_request, timeout=12)
    except (OSError, urllib.error.URLError, ssl.SSLError) as exc:
        raise ObservationError(f"HTTPS probe failed: {path}") from exc
    with response:
        if response.geturl() != url:
            raise ObservationError(f"HTTPS probe redirected: {path}")
        status = int(response.status)
        content_type = str(response.headers.get_content_type())
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ObservationError(f"HTTPS response exceeds limit: {path}")
    if not expect_json:
        return status, None
    if content_type != "application/json":
        raise ObservationError("prep health content type is not application/json")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObservationError("prep health is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ObservationError("prep health root is not an object")
    return status, value


def pm2_rows(pm2_bin: Path) -> list[dict]:
    if not pm2_bin.is_absolute():
        raise ObservationError("PM2 executable path is unsafe")
    requested = Path(os.path.abspath(pm2_bin))
    resolved = requested.resolve(strict=True)
    if requested != resolved:
        raise ObservationError("PM2 executable path must be canonical")
    info = resolved.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise ObservationError("PM2 executable metadata is unsafe")
    current = Path(resolved.anchor)
    for part in resolved.parts[1:-1]:
        current = current / part
        ancestor = current.lstat()
        if (
            not stat.S_ISDIR(ancestor.st_mode)
            or stat.S_ISLNK(ancestor.st_mode)
            or ancestor.st_uid != 0
            or stat.S_IMODE(ancestor.st_mode) & 0o022
        ):
            raise ObservationError("PM2 executable ancestor is unsafe")
    try:
        result = subprocess.run(
            [str(resolved), "jlist", "--silent"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "HOME": "/root",
                "USER": "root",
                "LOGNAME": "root",
                "PM2_HOME": "/root/.pm2",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ObservationError("PM2 query could not complete") from exc
    if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
        raise ObservationError("PM2 query failed")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ObservationError("PM2 query did not return strict JSON") from exc
    if not isinstance(document, list):
        raise ObservationError("PM2 query root is not a list")
    rows = []
    for name in PROCESS_NAMES:
        matches = [row for row in document if isinstance(row, dict) and row.get("name") == name]
        if len(matches) != 1:
            raise ObservationError(f"PM2 process must be unique: {name}")
        row = matches[0]
        env = row.get("pm2_env")
        if not isinstance(env, dict):
            raise ObservationError(f"PM2 definition is missing: {name}")
        pid = row.get("pid")
        restart_time = env.get("restart_time")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or isinstance(restart_time, bool)
            or not isinstance(restart_time, int)
            or restart_time < 0
            or env.get("status") != "online"
        ):
            raise ObservationError(f"PM2 process is not stably online: {name}")
        rows.append(
            {
                "name": name,
                "pid": pid,
                "restart_time": restart_time,
                "status": "online",
            }
        )
    return rows


def collect(origin: str, pm2_bin: Path) -> dict:
    homepage, _ = request(origin, "/")
    login, _ = request(origin, "/login")
    prep_status, prep = request(origin, "/prep/health", expect_json=True)
    if prep_status != 200 or prep is None:
        raise ObservationError("prep health HTTP status differs")
    health_ok = prep.get("ok") is True and prep.get("initialization") == "ready"
    database_ok = prep.get("database") == "ok"
    rows = pm2_rows(pm2_bin)
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    value = {
        "homepage_status": homepage,
        "login_status": login,
        "prep_health_ok": health_ok,
        "prep_database_ok": database_ok,
        "errors": 0,
        "restarts": 0,
        "pm2_fingerprint_sha256": hashlib.sha256(encoded).hexdigest(),
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        return validate_ordinary_oj(value)
    except EvidenceError as exc:
        raise ObservationError(str(exc)) from exc


def atomic_json(path: Path, value: dict) -> str:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        raise ObservationError("observation output must not already exist")
    require_private_output_parent(requested)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".v1-ordinary-oj-", dir=requested.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, requested)
        directory = os.open(requested.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--oj-origin", required=True)
    parser.add_argument("--pm2-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise ObservationError("ordinary OJ observation requires Linux root")
        if not HTTPS_ORIGIN.fullmatch(args.oj_origin):
            raise ObservationError("OJ origin must be one HTTPS origin without a path")
        value = collect(args.oj_origin, args.pm2_bin)
        digest = atomic_json(args.output, value)
        print(json.dumps({"observation_sha256": digest, "status": "passed"}, sort_keys=True))
        return 0
    except (ObservationError, EvidenceError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
