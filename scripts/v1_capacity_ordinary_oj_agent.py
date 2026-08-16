#!/usr/bin/env python3
"""Emit one signed, replay-resistant ordinary-OJ capacity observation."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


EMBEDDED_CONFIG = None
NAMESPACE = "noi-v1-capacity-ordinary-oj"
PROCESS_NAMES = ("caddy", "hydro-sandbox", "hydrooj", "mongodb")
HTTPS_ORIGIN = re.compile(r"https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]+)?")
SIGNER = re.compile(r"[A-Za-z0-9_.@+-]{1,80}")
HEX64 = re.compile(r"[a-f0-9]{64}")


class AgentError(RuntimeError):
    pass


def exact(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise AgentError(f"{label} field set differs")
    return value


def absolute(value: object, label: str) -> str:
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or "\x00" in value:
        raise AgentError(f"{label} must be an absolute path")
    if ".." in PurePosixPath(value).parts or "//" in value:
        raise AgentError(f"{label} must be normalized")
    return value


def validate_config(value: object) -> dict:
    row = exact(value, {
        "schema_version", "oj_origin", "public_paths", "prep_health_path", "pm2_bin",
        "pm2_baseline", "qualification_marker", "credential_canary", "result_canary", "signer",
        "signing_key_path", "ssh_keygen_path", "lock_path", "state_path", "output_path",
    }, "ordinary OJ agent configuration")
    if row["schema_version"] != 1 or not isinstance(row["oj_origin"], str) or \
            not HTTPS_ORIGIN.fullmatch(row["oj_origin"]):
        raise AgentError("ordinary OJ origin is invalid")
    paths = row["public_paths"]
    if not isinstance(paths, list) or not 2 <= len(paths) <= 8 or len(set(paths)) != len(paths) \
            or "/" not in paths or "/login" not in paths:
        raise AgentError("ordinary OJ public path set is invalid")
    if any(not isinstance(item, str) or not item.startswith("/") or "?" in item or "#" in item
           or len(item) > 256 for item in paths):
        raise AgentError("ordinary OJ public path is invalid")
    if row["prep_health_path"] != "/prep/health":
        raise AgentError("ordinary OJ prep health path differs")
    for key in ("pm2_bin", "signing_key_path", "ssh_keygen_path", "lock_path", "state_path", "output_path"):
        row[key] = absolute(row[key], key)
    baseline = row["pm2_baseline"]
    if not isinstance(baseline, list) or len(baseline) != 4:
        raise AgentError("ordinary OJ PM2 baseline must contain four processes")
    normalized = []
    for item in baseline:
        item = exact(item, {"name", "pid", "restart_time", "status"}, "PM2 baseline row")
        if item["name"] not in PROCESS_NAMES or item["status"] != "online" or \
                isinstance(item["pid"], bool) or not isinstance(item["pid"], int) or item["pid"] <= 0 or \
                isinstance(item["restart_time"], bool) or not isinstance(item["restart_time"], int) or \
                item["restart_time"] < 0:
            raise AgentError("ordinary OJ PM2 baseline row is invalid")
        normalized.append(dict(item))
    if sorted(item["name"] for item in normalized) != sorted(PROCESS_NAMES):
        raise AgentError("ordinary OJ PM2 baseline process set differs")
    row["pm2_baseline"] = sorted(normalized, key=lambda item: item["name"])
    if not isinstance(row["qualification_marker"], str) or not re.fullmatch(
            r"NOI-V1-QUAL-[A-Z0-9]{16,64}", row["qualification_marker"]
    ):
        raise AgentError("ordinary OJ qualification marker is invalid")
    for key in ("credential_canary", "result_canary"):
        kind = "CREDENTIAL" if key == "credential_canary" else "RESULT"
        if not isinstance(row[key], str) or not re.fullmatch(
                rf"NOI-V1-{kind}-[A-Z0-9]{{32,96}}", row[key]
        ):
            raise AgentError(f"{key} is invalid")
    if row["credential_canary"] == row["result_canary"]:
        raise AgentError("ordinary OJ leak canaries must differ")
    if not isinstance(row["signer"], str) or not SIGNER.fullmatch(row["signer"]):
        raise AgentError("ordinary OJ signer is invalid")
    if len({row["signing_key_path"], row["lock_path"], row["state_path"], row["output_path"]}) != 4:
        raise AgentError("ordinary OJ private paths must differ")
    return row


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def safe_ancestors(path: Path, *, leaf_owner: int = 0) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != leaf_owner \
                or stat.S_IMODE(info.st_mode) & 0o022:
            raise AgentError("ordinary OJ private path ancestor is unsafe")


def private_regular(
    path: Path, label: str, *, executable: bool = False, private: bool = False
) -> Path:
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    info = resolved.stat()
    safe_ancestors(resolved)
    if requested != resolved or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 \
            or stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022) \
            or (executable and not os.access(resolved, os.X_OK)):
        raise AgentError(f"{label} metadata is unsafe")
    return resolved


def request(origin: str, path: str, *, expect_json: bool = False) -> tuple[int, bytes, dict | None]:
    url = origin + path
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPRedirectHandler())
    req = urllib.request.Request(url, headers={"Accept": "application/json" if expect_json else "text/html"})
    try:
        response = opener.open(req, timeout=8)
    except (OSError, urllib.error.URLError, ssl.SSLError) as exc:
        raise AgentError("ordinary OJ HTTPS observation failed") from exc
    with response:
        if response.geturl() != url:
            raise AgentError("ordinary OJ HTTPS observation redirected")
        status = int(response.status)
        content_type = response.headers.get_content_type()
        raw = response.read(1024 * 1024 + 1)
    if status != 200 or len(raw) > 1024 * 1024:
        raise AgentError("ordinary OJ HTTPS status or response size differs")
    if not expect_json:
        return status, raw, None
    if content_type != "application/json":
        raise AgentError("ordinary OJ health content type differs")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentError("ordinary OJ health is not strict JSON") from exc
    if not isinstance(value, dict):
        raise AgentError("ordinary OJ health root differs")
    return status, raw, value


def pm2_rows(pm2_bin: Path) -> list[dict]:
    binary = private_regular(pm2_bin, "PM2 executable", executable=True)
    try:
        result = subprocess.run(
            [str(binary), "jlist", "--silent"], capture_output=True, check=False, timeout=20,
            env={"HOME": "/root", "USER": "root", "LOGNAME": "root", "PM2_HOME": "/root/.pm2",
                 "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentError("ordinary OJ PM2 query could not complete") from exc
    if result.returncode or result.stderr or len(result.stdout) > 8 * 1024 * 1024:
        raise AgentError("ordinary OJ PM2 query failed")
    try:
        document = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentError("ordinary OJ PM2 query is not strict JSON") from exc
    if not isinstance(document, list):
        raise AgentError("ordinary OJ PM2 query root differs")
    rows = []
    for name in PROCESS_NAMES:
        matches = [item for item in document if isinstance(item, dict) and item.get("name") == name]
        if len(matches) != 1 or not isinstance(matches[0].get("pm2_env"), dict):
            raise AgentError("ordinary OJ PM2 process identity differs")
        item, env = matches[0], matches[0]["pm2_env"]
        rows.append({"name": name, "pid": item.get("pid"), "restart_time": env.get("restart_time"),
                     "status": env.get("status")})
    return sorted(rows, key=lambda item: item["name"])


def next_sequence(path: Path) -> int:
    requested = Path(os.path.abspath(path))
    safe_ancestors(requested)
    current = 0
    if os.path.lexists(requested):
        info = requested.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or \
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
            raise AgentError("ordinary OJ sequence state metadata is unsafe")
        try:
            state = exact(json.loads(requested.read_text()), {"schema_version", "sequence"}, "sequence state")
            current = state["sequence"]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentError("ordinary OJ sequence state is invalid") from exc
        if state["schema_version"] != 1 or isinstance(current, bool) or not isinstance(current, int) or current < 1:
            raise AgentError("ordinary OJ sequence state is invalid")
    if current >= 2**53 - 2:
        raise AgentError("ordinary OJ sequence is exhausted")
    atomic_write(requested, canonical({"schema_version": 1, "sequence": current + 1}))
    return current + 1


def acquire_run_lock(path: Path) -> int:
    """Hold one non-blocking, no-follow lock across sequence allocation and publication."""
    import fcntl

    requested = Path(os.path.abspath(path))
    safe_ancestors(requested)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(requested, flags, 0o600)
    except OSError as exc:
        raise AgentError("ordinary OJ observer lock could not be opened safely") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or \
                stat.S_IMODE(info.st_mode) & 0o077:
            raise AgentError("ordinary OJ observer lock metadata is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AgentError("ordinary OJ observer is already running") from exc
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def atomic_write(path: Path, raw: bytes) -> None:
    parent = path.parent.resolve(strict=True)
    if parent != path.parent or parent.stat().st_uid != 0 or stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise AgentError("ordinary OJ output parent metadata is unsafe")
    safe_ancestors(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".noi-v1-ordinary-", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass


def collect(config: dict) -> dict:
    bodies = []
    for path in config["public_paths"]:
        _, raw, _ = request(config["oj_origin"], path)
        bodies.append(raw)
    _, health_raw, health = request(config["oj_origin"], config["prep_health_path"], expect_json=True)
    bodies.append(health_raw)
    assert health is not None
    if health.get("ok") is not True or health.get("database") != "ok" or health.get("initialization") != "ready":
        raise AgentError("ordinary OJ health semantics differ")
    rows = pm2_rows(Path(config["pm2_bin"]))
    if rows != config["pm2_baseline"]:
        raise AgentError("ordinary OJ PM2 identity, PID, restart count, or status changed")
    joined = b"\n".join(bodies)
    credential_leaks = int(config["credential_canary"].encode() in joined)
    result_leaks = int(config["result_canary"].encode() in joined)
    if credential_leaks or result_leaks:
        raise AgentError("ordinary OJ qualification canary was exposed")
    sequence = next_sequence(Path(config["state_path"]))
    return {
        "schema_version": 1, "qualification_marker": config["qualification_marker"],
        "sequence": sequence,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "homepage_status": 200, "login_status": 200, "prep_health_ok": True,
        "prep_database_ok": True, "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0,
        "ordinary_oj_pid_changes": 0, "credential_leaks": credential_leaks,
        "result_leaks": result_leaks,
        "pm2_fingerprint_sha256": hashlib.sha256(canonical(rows)).hexdigest(),
    }


def sign(config: dict, payload: dict) -> bytes:
    key = private_regular(
        Path(config["signing_key_path"]), "ordinary OJ signing key", private=True
    )
    binary = private_regular(Path(config["ssh_keygen_path"]), "ssh-keygen", executable=True)
    with tempfile.TemporaryDirectory(prefix="noi-v1-ordinary-sign-") as temporary:
        payload_path = Path(temporary) / "payload.json"
        payload_path.write_bytes(canonical(payload)); os.chmod(payload_path, 0o600)
        result = subprocess.run([str(binary), "-q", "-Y", "sign", "-f", str(key), "-n", NAMESPACE,
                                 str(payload_path)], capture_output=True, check=False, timeout=10)
        signature_path = Path(str(payload_path) + ".sig")
        if result.returncode or not signature_path.is_file():
            raise AgentError("ordinary OJ telemetry signing failed")
        return signature_path.read_bytes()


def main() -> int:
    previous_umask = None
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise AgentError("ordinary OJ capacity agent requires Linux root")
        previous_umask = os.umask(0o077)
        config = validate_config(EMBEDDED_CONFIG)
        lock_descriptor = acquire_run_lock(Path(config["lock_path"]))
        try:
            payload = collect(config)
            envelope = {"schema_version": 1, "namespace": NAMESPACE, "signer": config["signer"],
                        "payload": payload, "signature_base64": base64.b64encode(sign(config, payload)).decode()}
            atomic_write(Path(config["output_path"]), canonical(envelope))
        finally:
            os.close(lock_descriptor)
        print(json.dumps({"sequence": payload["sequence"], "status": "observed"}, sort_keys=True))
        return 0
    except (AgentError, OSError, TimeoutError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2
    finally:
        if previous_umask is not None:
            os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
