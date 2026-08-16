#!/usr/bin/env python3
"""Qualification-only controller stop/start recovery action with signed evidence."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
from urllib.parse import quote, urlsplit

EMBEDDED_CONFIG = None
NAMESPACE = "noi-v1-fault-recovery-actions"
HEX24 = re.compile(r"[a-f0-9]{24}")
HEX40 = re.compile(r"[a-f0-9]{40}")
HEX64 = re.compile(r"[a-f0-9]{64}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
MARKER = re.compile(r"NOI-V1-QUAL-[A-Z0-9]{16,64}")
PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?")


class AgentError(RuntimeError): pass


def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise AgentError(f"{label} field set differs")
    return value


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def absolute(value, label):
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or "\x00" in value or \
            ".." in PurePosixPath(value).parts or "//" in value:
        raise AgentError(f"{label} must be a normalized absolute path")
    return value


def validate_config(value):
    row = exact(value, {"schema_version", "qualification_marker", "session_id", "contest_id",
        "source", "components", "docker_socket", "controller", "database_path",
        "expected_pending_count", "expected_pending_set_sha256", "controller_health",
        "submission_status", "ordinary_oj", "signer", "signing_public_key", "signing_key_path",
        "ssh_keygen_path", "lock_path", "recovery_state_path", "receipt_path", "output_path"},
        "control restart configuration")
    if row["schema_version"] != 1 or not MARKER.fullmatch(str(row["qualification_marker"])) or \
            not HEX64.fullmatch(str(row["session_id"])) or not HEX24.fullmatch(str(row["contest_id"])) or \
            row["docker_socket"] != "/var/run/docker.sock":
        raise AgentError("control restart identity is invalid")
    source = exact(row["source"], {"revision", "tree"}, "source")
    components = exact(row["components"], {"orchestrator_image_digest", "desktop_image_id",
        "desktop_source_revision", "hydro_plugin_sha256"}, "components")
    if not HEX40.fullmatch(str(source["revision"])) or not HEX40.fullmatch(str(source["tree"])) or \
            components["desktop_source_revision"] != source["revision"] or \
            not IMAGE.fullmatch(str(components["orchestrator_image_digest"])) or \
            not IMAGE.fullmatch(str(components["desktop_image_id"])) or \
            not HEX64.fullmatch(str(components["hydro_plugin_sha256"])):
        raise AgentError("control restart source identity is invalid")
    controller = exact(row["controller"], {"container_id", "image_id", "name", "identity_sha256",
        "restart_count"}, "controller")
    if not HEX64.fullmatch(str(controller["container_id"])) or not IMAGE.fullmatch(str(controller["image_id"])) or \
            not re.fullmatch(r"/[A-Za-z0-9_.-]{1,127}", str(controller["name"])) or \
            not HEX64.fullmatch(str(controller["identity_sha256"])) or isinstance(controller["restart_count"], bool) or \
            not isinstance(controller["restart_count"], int) or controller["restart_count"] < 0 or \
            controller["image_id"] != components["orchestrator_image_digest"]:
        raise AgentError("control restart controller identity is invalid")
    if isinstance(row["expected_pending_count"], bool) or not isinstance(row["expected_pending_count"], int) or \
            not 1 <= row["expected_pending_count"] <= 100 or \
            not HEX64.fullmatch(str(row["expected_pending_set_sha256"])):
        raise AgentError("control restart pending set is invalid")
    health = exact(row["controller_health"], {"url", "timeout_seconds", "deadline_seconds"}, "controller health")
    status = exact(row["submission_status"], {"url", "token_path", "timeout_seconds"}, "submission status")
    for value, minimum, maximum, label in (
        (health["timeout_seconds"], 1, 10, "controller health timeout"),
        (health["deadline_seconds"], 10, 300, "controller health deadline"),
        (status["timeout_seconds"], 1, 10, "submission status timeout"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise AgentError(f"{label} is invalid")
    if not re.fullmatch(r"http://127[.]0[.]0[.]1:[0-9]+/healthz", str(health["url"])) or \
            not re.fullmatch(r"http://127[.]0[.]0[.]1:[0-9]+/orchestrator/submit/status", str(status["url"])):
        raise AgentError("control restart local endpoints are invalid")
    ordinary = exact(row["ordinary_oj"], {"pm2_path", "pm2_home", "processes", "http_probes"}, "ordinary OJ")
    if not isinstance(ordinary["processes"], list) or len(ordinary["processes"]) != 4 or \
            not isinstance(ordinary["http_probes"], list) or not 3 <= len(ordinary["http_probes"]) <= 6:
        raise AgentError("ordinary OJ baseline is invalid")
    names = set()
    for item in ordinary["processes"]:
        item = exact(item, {"name", "pid", "restart_time", "status"}, "ordinary OJ process")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(item["name"])) or item["name"] in names or \
                isinstance(item["pid"], bool) or not isinstance(item["pid"], int) or item["pid"] <= 0 or \
                isinstance(item["restart_time"], bool) or not isinstance(item["restart_time"], int) or \
                item["restart_time"] < 0 or item["status"] != "online":
            raise AgentError("ordinary OJ process baseline is invalid")
        names.add(item["name"])
    for item in ordinary["http_probes"]:
        item = exact(item, {"url", "host", "status", "body_contains"}, "ordinary OJ probe")
        parsed = urlsplit(str(item["url"]))
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or \
                not parsed.port or not parsed.path.startswith("/") or parsed.query or parsed.fragment or \
                not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", str(item["host"])) or \
                item["status"] != 200 or not isinstance(item["body_contains"], str) or len(item["body_contains"]) > 200:
            raise AgentError("ordinary OJ HTTP probe is invalid")
    for key in ("database_path", "signing_key_path", "ssh_keygen_path", "lock_path",
                "recovery_state_path", "receipt_path", "output_path"):
        row[key] = absolute(row[key], key)
    status["token_path"] = absolute(status["token_path"], "token_path")
    ordinary["pm2_path"] = absolute(ordinary["pm2_path"], "pm2_path")
    ordinary["pm2_home"] = absolute(ordinary["pm2_home"], "pm2_home")
    if len({row[k] for k in ("signing_key_path", "lock_path", "recovery_state_path", "receipt_path", "output_path")}) != 5 or \
            not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,80}", str(row["signer"])) or \
            not PUBLIC_KEY.fullmatch(str(row["signing_public_key"])):
        raise AgentError("control restart private paths or signer are invalid")
    return row


def safe_ancestors(path: Path):
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part; info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise AgentError("control restart path ancestor is unsafe")


def regular(path: Path, label, *, private=False, executable=False):
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True); info = resolved.stat(); safe_ancestors(resolved)
    if requested != resolved or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or \
            stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022) or (executable and not os.access(resolved, os.X_OK)):
        raise AgentError(f"{label} metadata is unsafe")
    return resolved


def atomic_write(path: Path, raw: bytes):
    parent = path.parent.resolve(strict=True); safe_ancestors(path); info = parent.stat()
    if parent != path.parent or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077 or os.path.lexists(path):
        raise AgentError("control restart output path is unsafe or already exists")
    descriptor, name = tempfile.mkstemp(prefix=".control-restart-", dir=parent); temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass


def unlink_durable(path: Path):
    try: path.unlink()
    except FileNotFoundError: return
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(directory)
    finally: os.close(directory)


def acquire_lock(path: Path):
    import fcntl
    safe_ancestors(path); descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
            raise AgentError("control restart lock metadata is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB); return descriptor
    except BaseException: os.close(descriptor); raise


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path): super().__init__("localhost", timeout=10); self.socket_path = socket_path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.sock.settimeout(self.timeout); self.sock.connect(self.socket_path)


def docker_request(config, method, path, *, expected):
    connection = UnixHTTPConnection(config["docker_socket"])
    try:
        connection.request(method, path); response = connection.getresponse(); raw = response.read(4 * 1024 * 1024 + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise AgentError("control restart Docker request failed") from exc
    finally: connection.close()
    if response.status not in expected or len(raw) > 4 * 1024 * 1024:
        raise AgentError("control restart Docker response differs")
    return raw


def immutable_identity(value):
    host = value.get("HostConfig") or {}; config = value.get("Config") or {}
    mounts = sorted(({"type": x.get("Type"), "source": x.get("Source"),
                      "destination": x.get("Destination"), "rw": x.get("RW")}
                     for x in value.get("Mounts") or []), key=lambda x: canonical(x))
    return {"container_id": value.get("Id"), "image_id": value.get("Image"), "name": value.get("Name"),
            "config_image": config.get("Image"), "entrypoint": config.get("Entrypoint"), "cmd": config.get("Cmd"),
            "restart_policy": host.get("RestartPolicy"), "mounts": mounts}


def inspect_controller(config, running=None):
    raw = docker_request(config, "GET", f"/containers/{quote(config['controller']['container_id'])}/json", expected={200})
    try: value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise AgentError("control restart Docker inspect is invalid") from exc
    state = value.get("State") or {}; expected = config["controller"]
    identity = immutable_identity(value); digest = hashlib.sha256(canonical(identity)).hexdigest()
    if value.get("Id") != expected["container_id"] or value.get("Image") != expected["image_id"] or \
            value.get("Name") != expected["name"] or value.get("RestartCount") != expected["restart_count"] or \
            digest != expected["identity_sha256"] or state.get("Restarting") is not False or \
            (running is not None and state.get("Running") is not running):
        raise AgentError("control restart controller identity or lifecycle differs")
    return value, digest


def wait_running(config, expected, deadline):
    end = time.monotonic() + deadline; last = None
    while time.monotonic() < end:
        try: last = inspect_controller(config, running=expected); return last
        except AgentError: time.sleep(1)
    raise AgentError("control restart controller lifecycle deadline expired")


def stop_controller(config):
    try: docker_request(config, "POST", f"/containers/{quote(config['controller']['container_id'])}/stop?t=30", expected={204, 304})
    except AgentError:
        pass
    return wait_running(config, False, 45)


def start_controller(config):
    try: docker_request(config, "POST", f"/containers/{quote(config['controller']['container_id'])}/start", expected={204, 304})
    except AgentError:
        pass
    return wait_running(config, True, 45)


def local_http(url, *, method="GET", host=None, body=None, headers=None, timeout=5):
    parsed = urlsplit(url); connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    merged = dict(headers or {})
    if host: merged["Host"] = host
    try:
        connection.request(method, parsed.path, body=body, headers=merged); response = connection.getresponse(); raw = response.read(2 * 1024 * 1024 + 1)
    except (OSError, http.client.HTTPException) as exc: raise AgentError("control restart local HTTP probe failed") from exc
    finally: connection.close()
    if len(raw) > 2 * 1024 * 1024: raise AgentError("control restart local HTTP response is too large")
    return response.status, raw


def health_ready(config):
    health = config["controller_health"]; end = time.monotonic() + health["deadline_seconds"]
    while time.monotonic() < end:
        try:
            status, raw = local_http(health["url"], timeout=health["timeout_seconds"])
            value = json.loads(raw)
            if status == 200 and value.get("ok") is True: return
        except (AgentError, UnicodeDecodeError, json.JSONDecodeError): pass
        time.sleep(1)
    raise AgentError("control restart controller did not become healthy")


def ordinary_snapshot(config):
    ordinary = config["ordinary_oj"]
    binary = regular(Path(ordinary["pm2_path"]), "pm2", executable=True)
    try:
        result = subprocess.run([str(binary), "jlist", "--silent"], capture_output=True, check=False, timeout=10,
            env={"HOME": "/root", "PM2_HOME": ordinary["pm2_home"], "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc: raise AgentError("ordinary OJ PM2 probe failed") from exc
    if result.returncode: raise AgentError("ordinary OJ PM2 probe failed")
    try: listing = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise AgentError("ordinary OJ PM2 response is invalid") from exc
    observed = []
    for expected in ordinary["processes"]:
        matches = [x for x in listing if x.get("name") == expected["name"]]
        if len(matches) != 1: raise AgentError("ordinary OJ PM2 identity differs")
        item = matches[0]; env = item.get("pm2_env") or {}
        current = {"name": item.get("name"), "pid": item.get("pid"), "restart_time": env.get("restart_time"), "status": env.get("status")}
        if current != expected: raise AgentError("ordinary OJ PM2 lifecycle changed")
        observed.append(current)
    for probe in ordinary["http_probes"]:
        status, raw = local_http(probe["url"], host=probe["host"], timeout=5)
        if status != probe["status"] or probe["body_contains"].encode() not in raw:
            raise AgentError("ordinary OJ HTTP probe differs")
    return observed


PENDING_COLUMNS = ("id", "tid", "uid", "problem", "sha256", "size", "submission_id", "submission_session",
                   "judge_pid", "judge_lang", "judge_sha256", "judge_state", "judge_kind", "accepted_at_ms", "rid")


def read_rows(config, *, ids=None):
    db = regular(Path(config["database_path"]), "orchestrator database", private=True)
    if "?" in str(db) or "#" in str(db): raise AgentError("orchestrator database path is not URI-safe")
    uri = f"file:{quote(db.as_posix(), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=3); connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        if ids is None:
            rows = connection.execute("SELECT " + ",".join(PENDING_COLUMNS) + " FROM web_submissions "
                "WHERE judge_state='pending' ORDER BY id").fetchall()
        else:
            placeholders = ",".join("?" for _ in ids)
            rows = connection.execute("SELECT " + ",".join(PENDING_COLUMNS) +
                f" FROM web_submissions WHERE id IN ({placeholders}) ORDER BY id", tuple(ids)).fetchall()
    finally: connection.close()
    return [dict(row) for row in rows]


def freeze_pending(config):
    rows = read_rows(config)
    if len(rows) != config["expected_pending_count"] or any(row["tid"] != config["contest_id"] for row in rows) or \
            len({row["submission_id"] for row in rows}) != len(rows) or \
            any(not HEX64.fullmatch(str(row["submission_id"])) for row in rows) or \
            hashlib.sha256(canonical(rows)).hexdigest() != config["expected_pending_set_sha256"]:
        raise AgentError("control restart pending delivery set differs")
    return rows


def wait_delivered(config, frozen):
    ids = [row["id"] for row in frozen]; end = time.monotonic() + config["controller_health"]["deadline_seconds"]
    while time.monotonic() < end:
        rows = read_rows(config, ids=ids)
        if len(rows) == len(frozen):
            immutable = {row["id"]: {key: value for key, value in row.items() if key not in {"judge_state", "rid"}}
                         for row in frozen}
            if any({key: value for key, value in row.items() if key not in {"judge_state", "rid"}} != immutable.get(row["id"])
                   for row in rows):
                raise AgentError("control restart delivery payload changed after restart")
            if all(row["judge_state"] == "submitted" and HEX24.fullmatch(str(row.get("rid", ""))) for row in rows):
                return rows
        time.sleep(1)
    raise AgentError("control restart pending deliveries did not recover")


def read_token(path):
    path = regular(Path(path), "submission status token", private=True); raw = path.read_bytes()
    if not 16 <= len(raw.strip()) <= 4096 or b"\x00" in raw: raise AgentError("submission status token is invalid")
    return raw.strip().decode()


def verify_unique_records(config, frozen, delivered):
    status_config = config["submission_status"]; token = read_token(status_config["token_path"])
    delivered_by_id = {row["id"]: row for row in delivered}; seen = set()
    for row in frozen:
        body = canonical({"submission_id": row["submission_id"]})
        status, raw = local_http(status_config["url"], method="POST", body=body,
            headers={"Content-Type": "application/json", "X-Orchestrator-Token": token}, timeout=status_config["timeout_seconds"])
        try: value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise AgentError("submission status response is invalid") from exc
        rid = str(delivered_by_id[row["id"]]["rid"])
        if status != 200 or value != {"status": "resolved", "rid": rid} or rid in seen:
            raise AgentError("control restart OJ record correlation is not unique")
        seen.add(rid)


def file_sha256(path):
    path = regular(path, "control restart frozen agent", executable=True); digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        while True:
            block = os.read(descriptor, 65536)
            if not block: break
            digest.update(block)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise AgentError("control restart frozen agent changed while hashing")
    finally: os.close(descriptor)
    return digest.hexdigest()


def sign(config, payload):
    key = regular(Path(config["signing_key_path"]), "control restart signing key", private=True)
    binary = regular(Path(config["ssh_keygen_path"]), "ssh-keygen", executable=True)
    with tempfile.TemporaryDirectory(prefix="noi-v1-control-restart-sign-") as directory:
        source = Path(directory) / "payload.json"; source.write_bytes(canonical(payload)); os.chmod(source, 0o600)
        result = subprocess.run([str(binary), "-q", "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(source)],
            capture_output=True, check=False, timeout=10)
        signature = Path(str(source) + ".sig")
        if result.returncode or not signature.is_file(): raise AgentError("control restart signing failed")
        return base64.b64encode(signature.read_bytes()).decode()


def verify_signature(config, payload, signature_base64):
    try: signature_raw = base64.b64decode(signature_base64, validate=True)
    except ValueError as exc: raise AgentError("control restart signature encoding is invalid") from exc
    binary = regular(Path(config["ssh_keygen_path"]), "ssh-keygen", executable=True)
    with tempfile.TemporaryDirectory(prefix="noi-v1-control-restart-verify-") as directory:
        allowed = Path(directory) / "allowed"; signature = Path(directory) / "payload.sig"
        allowed.write_text(f"{config['signer']} {config['signing_public_key']}\n", encoding="utf-8")
        signature.write_bytes(signature_raw); os.chmod(allowed, 0o600); os.chmod(signature, 0o600)
        try:
            result = subprocess.run([str(binary), "-Y", "verify", "-f", str(allowed), "-I", config["signer"],
                "-n", NAMESPACE, "-s", str(signature)], input=canonical(payload), capture_output=True,
                check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentError("control restart signature verification failed") from exc
    if result.returncode: raise AgentError("control restart signing key does not match the public key")


def run(config, *, runtime_path=None):
    row = validate_config(config); agent_sha = file_sha256(Path(__file__) if runtime_path is None else runtime_path)
    regular(Path(row["ssh_keygen_path"]), "ssh-keygen", executable=True)
    lock = acquire_lock(Path(row["lock_path"])); state_path = Path(row["recovery_state_path"]); stopped = False
    previous_handlers = {}
    def interrupted(signum, _frame): raise AgentError(f"control restart interrupted by signal {signum}")
    try:
        if os.path.lexists(state_path):
            start_controller(row); health_ready(row); unlink_durable(state_path)
            raise AgentError("stale control restart state was recovered; start a new run")
        if os.path.lexists(row["receipt_path"]) or os.path.lexists(row["output_path"]):
            raise AgentError("control restart output already exists")
        preflight_signature = {"schema_version": 1, "qualification_marker": row["qualification_marker"],
            "session_id": row["session_id"], "purpose": "preflight-signature-check"}
        verify_signature(row, preflight_signature, sign(row, preflight_signature))
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum); signal.signal(signum, interrupted)
        before_oj = ordinary_snapshot(row); _, identity_sha = inspect_controller(row, running=True); started = utc_now()
        atomic_write(state_path, canonical({"schema_version": 1, "qualification_marker": row["qualification_marker"],
            "container_id": row["controller"]["container_id"], "phase": "prepared"}))
        # Arm recovery before the Docker mutation. A timed-out HTTP response
        # may still be followed by a late daemon-side stop.
        stopped = True; stop_controller(row)
        frozen = freeze_pending(row); frozen_sha = hashlib.sha256(canonical(frozen)).hexdigest()
        start_controller(row); health_ready(row); stopped = False
        delivered = wait_delivered(row, frozen); verify_unique_records(row, frozen, delivered)
        _, post_identity_sha = inspect_controller(row, running=True); after_oj = ordinary_snapshot(row); ended = utc_now()
        if identity_sha != post_identity_sha or before_oj != after_oj: raise AgentError("control restart isolation baseline changed")
        receipt = {"schema_version": 1, "qualification_marker": row["qualification_marker"],
            "session_id": row["session_id"], "contest_id_sha256": hashlib.sha256(row["contest_id"].encode()).hexdigest(),
            "controller_identity_sha256": identity_sha, "pending_set_sha256": frozen_sha,
            "pending_count": len(frozen), "started_at": started, "ended_at": ended,
            "delivered_rid_set_sha256": hashlib.sha256(canonical(sorted(x["rid"] for x in delivered))).hexdigest()}
        receipt_raw = canonical(receipt); atomic_write(Path(row["receipt_path"]), receipt_raw)
        action = {"$schema": "v1-fault-recovery-action-fact.schema.json", "schema_version": 1,
            "kind": "fault_recovery_action", "scenario": "control_restart", "session_id": row["session_id"],
            "source": row["source"], "components": row["components"], "qualification_marker": row["qualification_marker"],
            "started_at": started, "ended_at": ended, "collector": {"mode": "trusted_action_agent", "agent_sha256": agent_sha},
            "signer": row["signer"], "signing_public_key": row["signing_public_key"],
            "payload": {"ordinary_oj_errors": 0, "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0,
                "duplicate_oj_records": 0, "final_source_mismatches": 0, "other_seat_failures": 0,
                "restart_events": 1, "restart_recoveries": 1, "pending_jobs_before": len(frozen),
                "pending_jobs_resumed": len(delivered), "controller_identity_preserved": True}}
        signature = sign(row, action); verify_signature(row, action, signature); action["signature"] = signature
        atomic_write(Path(row["output_path"]), canonical(action)); unlink_durable(state_path)
        return {"status": "passed", "pending_jobs_resumed": len(delivered),
                "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest()}
    finally:
        needs_start = stopped
        try:
            current, _ = inspect_controller(row)
            needs_start = current.get("State", {}).get("Running") is False
        except Exception:
            pass
        if needs_start:
            try:
                start_controller(row); health_ready(row); stopped = False
            except Exception:
                # Never claim success after an uncertain lifecycle. The durable
                # recovery marker remains for the next root-only recovery attempt.
                pass
        for signum, handler in previous_handlers.items(): signal.signal(signum, handler)
        os.close(lock)


def main():
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0: raise AgentError("control restart agent requires Linux root")
        if EMBEDDED_CONFIG is None: raise AgentError("control restart agent is not frozen")
        print(json.dumps(run(EMBEDDED_CONFIG), sort_keys=True, separators=(",", ":"))); return 0
    except (AgentError, OSError, sqlite3.Error, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        print(f"NO_GO: {exc}", file=os.sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
