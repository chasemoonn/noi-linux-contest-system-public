#!/usr/bin/env python3
"""Read-only fixed 15+2 capacity measurement probe runtime.

Do not execute this template directly.  build_v1_capacity_probe.py embeds the
reviewed site configuration into one root-owned executable.  Every invocation
combines a fresh host/container sample with signed, fresh browser telemetry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import argparse
import http.client
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import socket
import stat
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import quote


EMBEDDED_CONFIG = None
HEX64 = re.compile(r"^[0-9a-f]{64}$")
IFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")
SIGNER = re.compile(r"^[A-Za-z0-9_.@+-]{1,80}$")
SSH_PUBLIC_KEY = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?$")
NAMESPACE = "noi-v1-capacity-telemetry"
ORDINARY_OJ_NAMESPACE = "noi-v1-capacity-ordinary-oj"


class ProbeError(RuntimeError):
    pass


def exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ProbeError(f"{label} field set differs")
    return value


def number(value: Any, label: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProbeError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ProbeError(f"{label} is outside its allowed range")
    return result


def timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProbeError(f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProbeError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    return parsed


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def validate_config(value: Any) -> dict[str, Any]:
    row = exact_object(
        value,
        {
            "schema_version",
            "container_ids",
            "network_interface",
            "docker_socket",
            "telemetry_envelope",
            "qualification_marker",
            "telemetry_transport_profile",
            "telemetry_seat_set_sha256",
            "telemetry_signer",
            "telemetry_public_key",
            "ssh_keygen_path",
            "measurement_seconds",
            "telemetry_max_age_seconds",
            "telemetry_samples_min",
            "ordinary_oj_envelope",
            "ordinary_oj_signer",
            "ordinary_oj_public_key",
            "ordinary_oj_pm2_fingerprint_sha256",
            "ordinary_oj_max_age_seconds",
        },
        "probe configuration",
    )
    if row["schema_version"] != 1:
        raise ProbeError("probe configuration schema differs")
    ids = row["container_ids"]
    if (
        not isinstance(ids, list)
        or len(ids) != 17
        or len(set(ids)) != 17
        or any(not isinstance(item, str) or not HEX64.fullmatch(item) for item in ids)
    ):
        raise ProbeError("probe configuration must bind 17 unique full container IDs")
    if not isinstance(row["network_interface"], str) or not IFACE.fullmatch(row["network_interface"]):
        raise ProbeError("network interface is invalid")
    for key in ("docker_socket", "telemetry_envelope", "ordinary_oj_envelope", "ssh_keygen_path"):
        item = row[key]
        if (
            not isinstance(item, str)
            or not PurePosixPath(item).is_absolute()
            or ".." in PurePosixPath(item).parts
            or "\x00" in item
        ):
            raise ProbeError(f"{key} must be an absolute path")
    if row["docker_socket"] != "/var/run/docker.sock":
        raise ProbeError("only the canonical Docker socket is supported")
    if not isinstance(row["telemetry_signer"], str) or not SIGNER.fullmatch(row["telemetry_signer"]):
        raise ProbeError("telemetry signer identity is invalid")
    if not isinstance(row["qualification_marker"], str) or not re.fullmatch(
        r"NOI-V1-QUAL-[A-Z0-9]{16,64}", row["qualification_marker"]
    ):
        raise ProbeError("qualification marker is invalid")
    if row["telemetry_transport_profile"] != "direct_http":
        raise ProbeError("formal capacity telemetry must use the direct HTTP student path")
    if not isinstance(row["telemetry_seat_set_sha256"], str) or not HEX64.fullmatch(
        row["telemetry_seat_set_sha256"]
    ):
        raise ProbeError("browser telemetry seat set SHA256 is invalid")
    if not isinstance(row["telemetry_public_key"], str) or not SSH_PUBLIC_KEY.fullmatch(row["telemetry_public_key"]):
        raise ProbeError("telemetry public key must be one Ed25519 public key")
    if not isinstance(row["ordinary_oj_signer"], str) or not SIGNER.fullmatch(row["ordinary_oj_signer"]):
        raise ProbeError("ordinary OJ signer identity is invalid")
    if not isinstance(row["ordinary_oj_public_key"], str) or not SSH_PUBLIC_KEY.fullmatch(
        row["ordinary_oj_public_key"]
    ):
        raise ProbeError("ordinary OJ public key must be one Ed25519 public key")
    if not isinstance(row["ordinary_oj_pm2_fingerprint_sha256"], str) or not HEX64.fullmatch(
        row["ordinary_oj_pm2_fingerprint_sha256"]
    ):
        raise ProbeError("ordinary OJ PM2 fingerprint is invalid")
    seconds = row["measurement_seconds"]
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 2 <= seconds <= 5:
        raise ProbeError("measurement seconds must be between 2 and 5")
    age = row["telemetry_max_age_seconds"]
    if isinstance(age, bool) or not isinstance(age, int) or not 5 <= age <= 60:
        raise ProbeError("telemetry maximum age must be between 5 and 60 seconds")
    minimum = row["telemetry_samples_min"]
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 5 <= minimum <= 10000:
        raise ProbeError("telemetry sample minimum is invalid")
    ordinary_age = row["ordinary_oj_max_age_seconds"]
    if isinstance(ordinary_age, bool) or not isinstance(ordinary_age, int) or not 5 <= ordinary_age <= 60:
        raise ProbeError("ordinary OJ maximum age must be between 5 and 60 seconds")
    return row


def read_private_file(path: Path, label: str, limit: int) -> bytes:
    requested = Path(os.path.abspath(path))
    if requested != path:
        raise ProbeError(f"{label} path must be canonical and absolute")
    current = Path(requested.anchor)
    for part in requested.parts[1:-1]:
        current = current / part
        try:
            ancestor = current.lstat()
        except OSError as exc:
            raise ProbeError(f"{label} ancestor is unavailable") from exc
        if (
            platform.system().lower() == "linux"
            and (
                not stat.S_ISDIR(ancestor.st_mode)
                or stat.S_ISLNK(ancestor.st_mode)
                or ancestor.st_uid != 0
                or stat.S_IMODE(ancestor.st_mode) & 0o022
            )
        ):
            raise ProbeError(f"{label} ancestor is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProbeError(f"cannot open {label} safely") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size <= 0
            or info.st_size > limit
        ):
            raise ProbeError(f"{label} metadata is unsafe")
        raw = b""
        while len(raw) <= limit:
            block = os.read(descriptor, min(65536, limit + 1 - len(raw)))
            if not block:
                break
            raw += block
        if len(raw) != info.st_size:
            raise ProbeError(f"{label} changed while reading")
        return raw
    finally:
        os.close(descriptor)


def require_root_binary(path: Path, label: str) -> Path:
    requested = Path(os.path.abspath(path))
    try:
        resolved = requested.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise ProbeError(f"{label} is unavailable") from exc
    if (
        requested != resolved
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise ProbeError(f"{label} metadata is unsafe")
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
            raise ProbeError(f"{label} ancestor is unsafe")
    return resolved


def verify_telemetry(config: dict[str, Any], now: datetime) -> tuple[dict[str, Any], str]:
    envelope_raw = read_private_file(
        Path(config["telemetry_envelope"]), "browser telemetry envelope", 1024 * 1024
    )
    try:
        envelope_value = json.loads(envelope_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeError("browser telemetry envelope is not strict UTF-8 JSON") from exc
    envelope = exact_object(
        envelope_value,
        {"schema_version", "namespace", "signer", "payload", "signature_base64"},
        "browser telemetry envelope",
    )
    if (
        envelope_raw != canonical_json(envelope)
        or envelope["schema_version"] != 1
        or envelope["namespace"] != NAMESPACE
        or envelope["signer"] != config["telemetry_signer"]
    ):
        raise ProbeError("browser telemetry envelope identity differs")
    signature_value = envelope["signature_base64"]
    if not isinstance(signature_value, str) or not re.fullmatch(
        r"[A-Za-z0-9+/=]{40,131072}", signature_value
    ):
        raise ProbeError("browser telemetry envelope signature is invalid")
    import base64

    try:
        signature_raw = base64.b64decode(signature_value, validate=True)
    except ValueError as exc:
        raise ProbeError("browser telemetry envelope signature is invalid") from exc
    value = envelope["payload"]
    row = exact_object(
        value,
        {
            "schema_version",
            "qualification_marker",
            "seat_set_sha256",
            "transport_profile",
            "formal_seat_count",
            "sequence",
            "window_started_at",
            "observed_at",
            "rtt_samples_ms",
            "packet_loss_percent",
            "websocket_reconnects",
            "key_to_frame_samples_ms",
        },
        "browser telemetry",
    )
    raw = canonical_json(row)
    if row["schema_version"] != 1:
        raise ProbeError("browser telemetry schema version differs")
    if row["qualification_marker"] != config["qualification_marker"]:
        raise ProbeError("browser telemetry qualification marker differs")
    if row["seat_set_sha256"] != config["telemetry_seat_set_sha256"]:
        raise ProbeError("browser telemetry seat set SHA256 differs")
    if row["transport_profile"] != config["telemetry_transport_profile"]:
        raise ProbeError("browser telemetry transport profile differs")
    if row["formal_seat_count"] != 15:
        raise ProbeError("browser telemetry must bind 15 formal seats")
    if isinstance(row["sequence"], bool) or not isinstance(row["sequence"], int) or row["sequence"] < 1:
        raise ProbeError("browser telemetry sequence is invalid")
    started = timestamp(row["window_started_at"], "browser telemetry window start")
    observed = timestamp(row["observed_at"], "browser telemetry observed_at")
    age = (now - observed).total_seconds()
    window = (observed - started).total_seconds()
    if age < -5 or age > config["telemetry_max_age_seconds"] or not 1 <= window <= 60:
        raise ProbeError("browser telemetry is stale, future-dated, or has an invalid window")
    minimum = config["telemetry_samples_min"]
    for key in ("rtt_samples_ms", "key_to_frame_samples_ms"):
        values = row[key]
        if not isinstance(values, list) or not minimum <= len(values) <= 10000:
            raise ProbeError(f"browser telemetry {key} count is invalid")
        row[key] = [number(item, f"browser telemetry {key}", minimum=0.001) for item in values]
    loss = number(row["packet_loss_percent"], "browser packet loss")
    if loss > 100:
        raise ProbeError("browser packet loss exceeds 100 percent")
    reconnects = row["websocket_reconnects"]
    if isinstance(reconnects, bool) or not isinstance(reconnects, int) or reconnects < 0:
        raise ProbeError("browser websocket reconnect count is invalid")
    ssh_keygen = require_root_binary(Path(config["ssh_keygen_path"]), "ssh-keygen")
    with tempfile.TemporaryDirectory(prefix="noi-v1-telemetry-") as temporary:
        allowed = Path(temporary) / "allowed_signers"
        signature = Path(temporary) / "telemetry.sig"
        allowed.write_text(
            f"{config['telemetry_signer']} {config['telemetry_public_key']}\n",
            encoding="utf-8",
        )
        signature.write_bytes(signature_raw)
        os.chmod(allowed, 0o600)
        os.chmod(signature, 0o600)
        try:
            result = subprocess.run(
                [
                    str(ssh_keygen), "-Y", "verify", "-f", str(allowed),
                    "-I", config["telemetry_signer"], "-n", NAMESPACE,
                    "-s", str(signature),
                ],
                input=raw,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProbeError("browser telemetry signature verification could not complete") from exc
    if result.returncode != 0:
        raise ProbeError("browser telemetry signature is invalid")
    import hashlib

    return row, hashlib.sha256(raw).hexdigest()


def verify_ordinary_oj(config: dict[str, Any], now: datetime) -> tuple[dict[str, Any], str]:
    envelope_raw = read_private_file(
        Path(config["ordinary_oj_envelope"]), "ordinary OJ telemetry envelope", 1024 * 1024
    )
    try:
        value = json.loads(envelope_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeError("ordinary OJ telemetry envelope is not strict UTF-8 JSON") from exc
    envelope = exact_object(
        value, {"schema_version", "namespace", "signer", "payload", "signature_base64"},
        "ordinary OJ telemetry envelope",
    )
    if envelope_raw != canonical_json(envelope) or envelope["schema_version"] != 1 or \
            envelope["namespace"] != ORDINARY_OJ_NAMESPACE or \
            envelope["signer"] != config["ordinary_oj_signer"]:
        raise ProbeError("ordinary OJ telemetry envelope identity differs")
    signature_value = envelope["signature_base64"]
    if not isinstance(signature_value, str) or not re.fullmatch(r"[A-Za-z0-9+/=]{40,131072}", signature_value):
        raise ProbeError("ordinary OJ telemetry signature is invalid")
    import base64
    try:
        signature_raw = base64.b64decode(signature_value, validate=True)
    except ValueError as exc:
        raise ProbeError("ordinary OJ telemetry signature is invalid") from exc
    row = exact_object(
        envelope["payload"], {
            "schema_version", "qualification_marker", "sequence", "observed_at", "homepage_status", "login_status",
            "prep_health_ok", "prep_database_ok", "ordinary_oj_errors", "ordinary_oj_restarts",
            "ordinary_oj_pid_changes", "credential_leaks", "result_leaks",
            "pm2_fingerprint_sha256",
        }, "ordinary OJ telemetry",
    )
    raw = canonical_json(row)
    if row["schema_version"] != 1 or row["qualification_marker"] != config["qualification_marker"] or \
            isinstance(row["sequence"], bool) or \
            not isinstance(row["sequence"], int) or row["sequence"] < 1:
        raise ProbeError("ordinary OJ telemetry identity is invalid")
    observed = timestamp(row["observed_at"], "ordinary OJ telemetry observed_at")
    age = (now - observed).total_seconds()
    if age < -5 or age > config["ordinary_oj_max_age_seconds"]:
        raise ProbeError("ordinary OJ telemetry is stale or future-dated")
    if row["homepage_status"] != 200 or row["login_status"] != 200 or \
            row["prep_health_ok"] is not True or row["prep_database_ok"] is not True:
        raise ProbeError("ordinary OJ HTTP or health state differs")
    for key in ("ordinary_oj_errors", "ordinary_oj_restarts", "ordinary_oj_pid_changes",
                "credential_leaks", "result_leaks"):
        if row[key] != 0:
            raise ProbeError(f"ordinary OJ telemetry {key} is non-zero")
    if row["pm2_fingerprint_sha256"] != config["ordinary_oj_pm2_fingerprint_sha256"]:
        raise ProbeError("ordinary OJ PM2 fingerprint differs")
    ssh_keygen = require_root_binary(Path(config["ssh_keygen_path"]), "ssh-keygen")
    with tempfile.TemporaryDirectory(prefix="noi-v1-ordinary-verify-") as temporary:
        allowed = Path(temporary) / "allowed_signers"
        signature = Path(temporary) / "telemetry.sig"
        allowed.write_text(
            f"{config['ordinary_oj_signer']} {config['ordinary_oj_public_key']}\n", encoding="utf-8"
        )
        signature.write_bytes(signature_raw)
        os.chmod(allowed, 0o600); os.chmod(signature, 0o600)
        try:
            result = subprocess.run(
                [str(ssh_keygen), "-Y", "verify", "-f", str(allowed), "-I",
                 config["ordinary_oj_signer"], "-n", ORDINARY_OJ_NAMESPACE,
                 "-s", str(signature)], input=raw, capture_output=True, check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProbeError("ordinary OJ telemetry signature verification could not complete") from exc
    if result.returncode != 0:
        raise ProbeError("ordinary OJ telemetry signature is invalid")
    import hashlib
    return row, hashlib.sha256(raw).hexdigest()


def percentile(values: list[float], percentage: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentage) - 1)
    return ordered[index]


def cpu_counters() -> tuple[int, int]:
    try:
        fields = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
        values = [int(item) for item in fields[1:]]
    except (OSError, ValueError, IndexError) as exc:
        raise ProbeError("host CPU counters are unavailable") from exc
    if len(values) < 5:
        raise ProbeError("host CPU counters are incomplete")
    idle = values[3] + values[4]
    return sum(values), idle


def memory_percent() -> float:
    try:
        rows = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, rest = line.split(":", 1)
            rows[key] = int(rest.split()[0])
        total, available = rows["MemTotal"], rows["MemAvailable"]
    except (OSError, ValueError, KeyError) as exc:
        raise ProbeError("host memory counters are unavailable") from exc
    if total <= 0 or not 0 <= available <= total:
        raise ProbeError("host memory counters are invalid")
    return (total - available) * 100.0 / total


def network_tx_bytes(interface: str) -> int:
    try:
        value = int(Path(f"/sys/class/net/{interface}/statistics/tx_bytes").read_text().strip())
    except (OSError, ValueError) as exc:
        raise ProbeError("network transmit counter is unavailable") from exc
    if value < 0:
        raise ProbeError("network transmit counter is invalid")
    return value


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: int = 12):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def docker_inspect(socket_path: str, container_id: str) -> int:
    connection = UnixHTTPConnection(socket_path)
    try:
        connection.request("GET", f"/containers/{quote(container_id)}/json")
        response = connection.getresponse()
        raw = response.read(4 * 1024 * 1024 + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise ProbeError("Docker statistics query failed") from exc
    finally:
        connection.close()
    if response.status != 200 or len(raw) > 4 * 1024 * 1024:
        raise ProbeError("Docker statistics response is invalid")
    try:
        value = json.loads(raw)
        observed_id = value["Id"]
        state = value["State"]
        pid = state["Pid"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProbeError("Docker inspection document is invalid") from exc
    if (
        observed_id != container_id
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or state.get("Restarting") is not False
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
    ):
        raise ProbeError("Docker container identity or running state differs")
    return pid


def cgroup_memory_path(pid: int, container_id: str) -> Path:
    try:
        rows = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise ProbeError("container cgroup membership is unavailable") from exc
    candidates: list[Path] = []
    for row in rows:
        parts = row.split(":", 2)
        if len(parts) != 3:
            raise ProbeError("container cgroup membership is malformed")
        controllers, relative = parts[1], parts[2]
        if ".." in Path(relative).parts:
            raise ProbeError("container cgroup path is unsafe")
        if parts[0] == "0" and controllers == "":
            candidates.append(Path("/sys/fs/cgroup") / relative.lstrip("/") / "memory.current")
        elif "memory" in controllers.split(","):
            candidates.append(
                Path("/sys/fs/cgroup/memory")
                / relative.lstrip("/")
                / "memory.usage_in_bytes"
            )
    existing = [path for path in candidates if path.is_file()]
    if len(existing) != 1 or container_id not in existing[0].as_posix():
        raise ProbeError("container cgroup memory counter is ambiguous")
    root = Path("/sys/fs/cgroup").resolve()
    resolved = existing[0].resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ProbeError("container cgroup memory counter escapes its root") from exc
    return resolved


def cgroup_memory_bytes(path: Path) -> int:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError) as exc:
        raise ProbeError("container memory counter is unavailable") from exc
    if value <= 0:
        raise ProbeError("container memory counter is invalid")
    return value


def collect(config: dict[str, Any], *, sleep=time.sleep) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    telemetry, telemetry_sha256 = verify_telemetry(config, started_at)
    ordinary_oj, ordinary_oj_sha256 = verify_ordinary_oj(config, started_at)
    pids = {
        container_id: docker_inspect(config["docker_socket"], container_id)
        for container_id in config["container_ids"]
    }
    counters = {
        container_id: cgroup_memory_path(pid, container_id)
        for container_id, pid in pids.items()
    }
    cpu_previous = cpu_counters()
    tx_previous = network_tx_bytes(config["network_interface"])
    cpu_samples: list[float] = []
    memory_samples = [memory_percent()]
    egress_samples: list[float] = []
    container_memory_samples = [
        cgroup_memory_bytes(path) for path in counters.values()
    ]
    for _ in range(config["measurement_seconds"]):
        sleep(1)
        cpu_current = cpu_counters()
        tx_current = network_tx_bytes(config["network_interface"])
        total_delta = cpu_current[0] - cpu_previous[0]
        idle_delta = cpu_current[1] - cpu_previous[1]
        tx_delta = tx_current - tx_previous
        if total_delta <= 0 or idle_delta < 0 or tx_delta < 0:
            raise ProbeError("host sample counters moved backwards or did not advance")
        cpu_samples.append((total_delta - idle_delta) * 100.0 / total_delta)
        egress_samples.append(tx_delta * 8.0 / 1_000_000)
        memory_samples.append(memory_percent())
        container_memory_samples.extend(
            cgroup_memory_bytes(path) for path in counters.values()
        )
        cpu_previous, tx_previous = cpu_current, tx_current
    for container_id, expected_pid in pids.items():
        if docker_inspect(config["docker_socket"], container_id) != expected_pid:
            raise ProbeError("a target container changed while measuring")
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "observed_at": observed_at,
        "telemetry": {
            "sequence": telemetry["sequence"],
            "sha256": telemetry_sha256,
        },
        "ordinary_oj": {
            **ordinary_oj,
            "sha256": ordinary_oj_sha256,
        },
        "metrics": {
            "host_cpu_peak_percent": round(max(cpu_samples), 3),
            "host_memory_peak_percent": round(max(memory_samples), 3),
            "container_memory_peak_bytes": max(container_memory_samples),
            "egress_peak_mbps": round(max(egress_samples), 3),
            "rtt_p95_ms": round(percentile(telemetry["rtt_samples_ms"], 0.95), 3),
            "packet_loss_percent": telemetry["packet_loss_percent"],
            "websocket_reconnects": telemetry["websocket_reconnects"],
            "key_to_frame_p50_ms": round(percentile(telemetry["key_to_frame_samples_ms"], 0.50), 3),
            "key_to_frame_p95_ms": round(percentile(telemetry["key_to_frame_samples_ms"], 0.95), 3),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise ProbeError("capacity measurement probe requires Linux root")
        config = validate_config(EMBEDDED_CONFIG)
        value = collect(config)
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    except (ProbeError, OSError, TimeoutError) as exc:
        # The parent collector requires empty stderr on success and treats this
        # generic failure as NO-GO.  Never print paths, telemetry, or identities.
        print(f"NO_GO: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
