#!/usr/bin/env python3
"""Run one bounded controller-only network interruption and sign its evidence."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote


EMBEDDED_CONFIG = None
NAMESPACE = "noi-v1-capacity-network-fault"
HEX24 = re.compile(r"[a-f0-9]{24}")
HEX64 = re.compile(r"[a-f0-9]{64}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.]\d+)?Z")
SIGNER = re.compile(r"[A-Za-z0-9_.@+-]{1,80}")
SSH_PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?")


class AgentError(RuntimeError):
    pass


def exact(value, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise AgentError(f"{label} field set differs")
    return value


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def absolute(value, label: str) -> str:
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or \
            "\x00" in value or ".." in PurePosixPath(value).parts or "//" in value:
        raise AgentError(f"{label} must be a normalized absolute path")
    return value


def validate_config(value) -> dict:
    row = exact(value, {
        "schema_version", "qualification_marker", "contest_id",
        "seat_inventory_probe_sha256", "docker_socket", "controller",
        "target_ipv4", "target_port", "controller_probe_target_sha256",
        "nsenter_path", "iptables_path", "python_path", "signer", "signing_public_key",
        "signing_key_path", "ssh_keygen_path", "lock_path", "recovery_state_path",
        "receipt_path", "output_path",
    }, "network fault agent configuration")
    if row["schema_version"] != 1 or not isinstance(row["qualification_marker"], str) or \
            not re.fullmatch(r"NOI-V1-QUAL-[A-Z0-9]{16,64}", row["qualification_marker"]) or \
            not isinstance(row["contest_id"], str) or not HEX24.fullmatch(row["contest_id"]) or \
            not isinstance(row["seat_inventory_probe_sha256"], str) or \
            not HEX64.fullmatch(row["seat_inventory_probe_sha256"]) or \
            row["docker_socket"] != "/var/run/docker.sock":
        raise AgentError("network fault agent identity is invalid")
    controller = exact(row["controller"], {
        "container_id", "image_id", "pid", "started_at", "restart_count"
    }, "network fault controller")
    if not isinstance(controller["container_id"], str) or not HEX64.fullmatch(controller["container_id"]) or \
            not isinstance(controller["image_id"], str) or \
            not re.fullmatch(r"sha256:[a-f0-9]{64}", controller["image_id"]) or \
            isinstance(controller["pid"], bool) or not isinstance(controller["pid"], int) or controller["pid"] <= 0 or \
            not isinstance(controller["started_at"], str) or not TIMESTAMP.fullmatch(controller["started_at"]) or \
            isinstance(controller["restart_count"], bool) or not isinstance(controller["restart_count"], int) or \
            controller["restart_count"] < 0:
        raise AgentError("network fault controller identity is invalid")
    try:
        address = ipaddress.ip_address(row["target_ipv4"])
    except (TypeError, ValueError) as exc:
        raise AgentError("network fault target IPv4 is invalid") from exc
    if address.version != 4 or address.is_loopback or address.is_multicast or address.is_unspecified or \
            isinstance(row["target_port"], bool) or not isinstance(row["target_port"], int) or \
            not 1 <= row["target_port"] <= 65535:
        raise AgentError("network fault target endpoint is invalid")
    target_digest = hashlib.sha256(canonical({
        "ipv4": str(address), "port": row["target_port"]
    })).hexdigest()
    if row["controller_probe_target_sha256"] != target_digest:
        raise AgentError("network fault target SHA256 differs")
    for key in ("nsenter_path", "iptables_path", "python_path", "signing_key_path",
                "ssh_keygen_path", "lock_path", "recovery_state_path", "receipt_path", "output_path"):
        row[key] = absolute(row[key], key)
    if len({row[key] for key in (
            "signing_key_path", "lock_path", "recovery_state_path", "receipt_path", "output_path")}) != 5 or \
            not isinstance(row["signer"], str) or not SIGNER.fullmatch(row["signer"]) or \
            not isinstance(row["signing_public_key"], str) or \
            not SSH_PUBLIC_KEY.fullmatch(row["signing_public_key"]):
        raise AgentError("network fault private paths or signer are invalid")
    return row


def safe_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part; info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or \
                stat.S_IMODE(info.st_mode) & 0o022:
            raise AgentError("network fault private path ancestor is unsafe")


def private_regular(path: Path, label: str, *, executable=False, private=False) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    info = resolved.stat(); safe_ancestors(resolved)
    if requested != resolved or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or \
            stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022) or \
            (executable and not os.access(resolved, os.X_OK)):
        raise AgentError(f"{label} metadata is unsafe")
    return resolved


def atomic_write(path: Path, raw: bytes) -> None:
    parent = path.parent.resolve(strict=True); safe_ancestors(path)
    info = parent.stat()
    if parent != path.parent or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise AgentError("network fault output parent is unsafe")
    descriptor, name = tempfile.mkstemp(prefix=".network-fault-", dir=parent)
    temporary = Path(name)
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


def file_sha256(path: Path, label: str) -> str:
    path = private_regular(path, label, executable=True)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)); digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        while True:
            block = os.read(descriptor, 65536)
            if not block: break
            digest.update(block)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != \
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise AgentError(f"{label} changed while hashing")
    finally: os.close(descriptor)
    return digest.hexdigest()


def unlink_durable(path: Path) -> None:
    try: path.unlink()
    except FileNotFoundError: return
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(directory)
    finally: os.close(directory)


def assert_output_absent(path: Path, label: str) -> None:
    """Reject every pre-existing inode, including dangling links and special files."""
    if os.path.lexists(path):
        raise AgentError(f"{label} already exists")


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost", timeout=10); self.socket_path = socket_path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout); self.sock.connect(self.socket_path)


def controller_inspect(config: dict) -> dict:
    connection = UnixHTTPConnection(config["docker_socket"])
    try:
        connection.request("GET", f"/containers/{quote(config['controller']['container_id'])}/json")
        response = connection.getresponse(); raw = response.read(4 * 1024 * 1024 + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise AgentError("network fault Docker identity query failed") from exc
    finally: connection.close()
    if response.status != 200 or not raw or len(raw) > 4 * 1024 * 1024:
        raise AgentError("network fault Docker identity response differs")
    try: value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentError("network fault Docker identity is not JSON") from exc
    state = value.get("State") or {}; expected = config["controller"]
    observed = {"container_id": value.get("Id"), "image_id": value.get("Image"),
                "pid": state.get("Pid"), "started_at": state.get("StartedAt"),
                "restart_count": value.get("RestartCount")}
    if observed != expected or state.get("Running") is not True or state.get("Restarting") is not False:
        raise AgentError("network fault controller lifecycle changed")
    return observed


PROBE_CODE = (
    "import socket,sys; s=socket.create_connection((sys.argv[1],int(sys.argv[2])),"
    "timeout=float(sys.argv[3])); s.close()"
)


def rule_args(config: dict) -> list[str]:
    return ["OUTPUT", "-p", "tcp", "-d", config["target_ipv4"], "--dport",
            str(config["target_port"]), "-m", "comment", "--comment",
            config["qualification_marker"], "-j", "REJECT", "--reject-with", "tcp-reset"]


def ns_command(config: dict, argv: list[str], *, ok=(0,)) -> subprocess.CompletedProcess:
    controller_inspect(config)
    command = [config["nsenter_path"], "--target", str(config["controller"]["pid"]),
               "--net"] + argv
    try: result = subprocess.run(command, capture_output=True, check=False, timeout=10,
                                 env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentError("network fault namespace command could not complete") from exc
    if result.returncode not in ok:
        raise AgentError("network fault namespace command failed")
    return result


def rule_present(config: dict) -> bool:
    result = ns_command(config, [config["iptables_path"], "--wait", "5", "-C"] + rule_args(config), ok=(0, 1))
    return result.returncode == 0


def install_rule(config: dict) -> None:
    if rule_present(config): raise AgentError("network fault rule already exists")
    ns_command(config, [config["iptables_path"], "--wait", "5", "-I", "OUTPUT", "1"] + rule_args(config)[1:])
    if not rule_present(config): raise AgentError("network fault rule was not installed")


def remove_rule(config: dict) -> None:
    if rule_present(config):
        ns_command(config, [config["iptables_path"], "--wait", "5", "-D"] + rule_args(config))
    if rule_present(config): raise AgentError("network fault rule remains installed")


def probe_target(config: dict, expect_success: bool) -> None:
    for _ in range(3):
        result = ns_command(config, [config["python_path"], "-I", "-c", PROBE_CODE,
                            config["target_ipv4"], str(config["target_port"]), "2"], ok=(0, 1))
        if (result.returncode == 0) is not expect_success:
            raise AgentError("network fault target probe result differs")
        time.sleep(1)


def acquire_lock(path: Path) -> int:
    import fcntl
    safe_ancestors(path); flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or \
                stat.S_IMODE(info.st_mode) & 0o077:
            raise AgentError("network fault lock metadata is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB); return descriptor
    except BaseException: os.close(descriptor); raise


def sign(config: dict, payload: dict) -> str:
    key = private_regular(Path(config["signing_key_path"]), "network fault signing key", private=True)
    binary = private_regular(Path(config["ssh_keygen_path"]), "ssh-keygen", executable=True)
    with tempfile.TemporaryDirectory(prefix="noi-v1-network-sign-") as directory:
        source = Path(directory) / "payload.json"; source.write_bytes(canonical(payload)); os.chmod(source, 0o600)
        try: result = subprocess.run([str(binary), "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(source)],
                                     capture_output=True, check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentError("network fault signature could not be created") from exc
        signature = Path(str(source) + ".sig")
        if result.returncode or not signature.is_file(): raise AgentError("network fault signing failed")
        return base64.b64encode(signature.read_bytes()).decode()


def verify_signature(config: dict, payload: dict, signature_base64: str) -> None:
    try: signature_raw = base64.b64decode(signature_base64, validate=True)
    except ValueError as exc: raise AgentError("network fault signature encoding is invalid") from exc
    binary = private_regular(Path(config["ssh_keygen_path"]), "ssh-keygen", executable=True)
    with tempfile.TemporaryDirectory(prefix="noi-v1-network-verify-") as directory:
        allowed = Path(directory) / "allowed_signers"; signature = Path(directory) / "payload.sig"
        allowed.write_text(f"{config['signer']} {config['signing_public_key']}\n")
        signature.write_bytes(signature_raw); os.chmod(allowed, 0o600); os.chmod(signature, 0o600)
        try: result = subprocess.run(
            [str(binary), "-Y", "verify", "-f", str(allowed), "-I", config["signer"],
             "-n", NAMESPACE, "-s", str(signature)], input=canonical(payload),
            capture_output=True, check=False, timeout=10,
        )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentError("network fault signature verification could not complete") from exc
    if result.returncode: raise AgentError("network fault signing key does not match public key")


def run(config: dict, *, runtime_path: Path | None = None) -> dict:
    row = validate_config(config)
    agent_sha = file_sha256(Path(__file__) if runtime_path is None else runtime_path,
                            "network fault frozen agent")
    for key in ("nsenter_path", "iptables_path", "python_path"):
        private_regular(Path(row[key]), key, executable=True)
    lock = acquire_lock(Path(row["lock_path"])); state_path = Path(row["recovery_state_path"])
    previous_handlers = {}
    def interrupted(signum, _frame):
        raise AgentError(f"network fault agent interrupted by signal {signum}")
    try:
        if os.path.lexists(state_path):
            remove_rule(row); unlink_durable(state_path)
            raise AgentError("stale network fault state was recovered; start a new run")
        assert_output_absent(Path(row["receipt_path"]), "network fault receipt output")
        assert_output_absent(Path(row["output_path"]), "network fault signed output")
        preflight = {"schema_version": 1, "qualification_marker": row["qualification_marker"],
                     "purpose": "preflight-signature-check"}
        verify_signature(row, preflight, sign(row, preflight))
        for signum in tuple(
            item for item in (
                getattr(signal, "SIGHUP", None), signal.SIGINT, signal.SIGTERM
            ) if item is not None
        ):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupted)
        controller = controller_inspect(row)
        identity_sha = hashlib.sha256(canonical(controller)).hexdigest()
        rule_sha = hashlib.sha256(canonical({"args": rule_args(row)})).hexdigest()
        started = utc_now(); probe_target(row, True); before = utc_now()
        state = {"schema_version": 1, "qualification_marker": row["qualification_marker"],
                 "controller_identity_sha256": identity_sha, "rule_identity_sha256": rule_sha,
                 "phase": "prepared"}
        atomic_write(state_path, canonical(state)); installed = False
        try:
            install_rule(row); installed = True; installed_at = utc_now()
            state["phase"] = "installed"; atomic_write(state_path, canonical(state))
            probe_target(row, False); during = utc_now()
        finally:
            if installed or rule_present(row): remove_rule(row)
        removed_at = utc_now(); probe_target(row, True); after = utc_now(); controller_inspect(row)
        completed = utc_now()
        receipt = {"schema_version": 1, "qualification_marker": row["qualification_marker"],
                   "contest_id_sha256": hashlib.sha256(row["contest_id"].encode()).hexdigest(),
                   "seat_inventory_probe_sha256": row["seat_inventory_probe_sha256"],
                   "controller_probe_target_sha256": row["controller_probe_target_sha256"],
                   "agent_sha256": agent_sha, "controller_identity_sha256": identity_sha, "rule_identity_sha256": rule_sha,
                   "started_at": started, "rule_installed_at": installed_at,
                   "rule_removed_at": removed_at, "completed_at": completed,
                   "before_successes": 3, "during_failures": 3, "after_successes": 3}
        receipt_raw = canonical(receipt); atomic_write(Path(row["receipt_path"]), receipt_raw)
        payload = {"schema_version": 1, "qualification_marker": row["qualification_marker"],
                   "contest_id_sha256": receipt["contest_id_sha256"],
                   "seat_inventory_probe_sha256": row["seat_inventory_probe_sha256"],
                   "controller_probe_target_sha256": row["controller_probe_target_sha256"],
                   "fault_method": "controller-egress-deny",
                   "operation_receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
                   "observed_at": completed,
                   "events": [
                       {"phase": "before_interrupt", "observed_at": before,
                        "consecutive_probe_successes": 3, "consecutive_probe_failures": 0},
                       {"phase": "during_interrupt", "observed_at": during,
                        "consecutive_probe_successes": 0, "consecutive_probe_failures": 3},
                       {"phase": "after_recovery", "observed_at": after,
                        "consecutive_probe_successes": 3, "consecutive_probe_failures": 0}]}
        signature = sign(row, payload); verify_signature(row, payload, signature)
        envelope = {"schema_version": 1, "namespace": NAMESPACE, "signer": row["signer"],
                    "payload": payload, "signature_base64": signature}
        atomic_write(Path(row["output_path"]), canonical(envelope)); unlink_durable(state_path)
        return {"status": "passed", "receipt_sha256": payload["operation_receipt_sha256"]}
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        os.close(lock)


def main() -> int:
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise AgentError("network fault agent requires Linux root")
        if EMBEDDED_CONFIG is None: raise AgentError("network fault agent is not frozen")
        result = run(EMBEDDED_CONFIG)
        print(json.dumps(result, sort_keys=True, separators=(",", ":"))); return 0
    except (AgentError, OSError, json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
