#!/usr/bin/env python3
"""Build append-only, root-only raw evidence for the V1 15+2 qualification.

The collector never restarts a service, changes a network rule, or calls a
cloud write API.  Operators and dedicated probes perform the documented
workload/fault exercises; this tool freezes their privacy-safe observations,
checks source identity on every step, and derives the combined evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from typing import Any

from verify_v1_capacity_evidence import (
    ARTIFACT_NAMES,
    DIGEST,
    EvidenceError,
    POST_SHUTDOWN_FACT_SECONDS,
    PRE_COLLECTION_FACT_SECONDS,
    HEX40,
    HEX64,
    PROFILE,
    SAFE_TEXT,
    THRESHOLD_VALUE_NAMES,
    exact_keys,
    parse_utc,
    require_number,
    require_pattern,
    threshold_policy_sha256,
    validate_capacity_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
METRIC_NAMES = {
    "host_cpu_peak_percent",
    "host_memory_peak_percent",
    "container_memory_peak_bytes",
    "egress_peak_mbps",
    "rtt_p95_ms",
    "packet_loss_percent",
    "websocket_reconnects",
    "key_to_frame_p50_ms",
    "key_to_frame_p95_ms",
}
FACT_KINDS = {
    "seat_inventory",
    "workload_events",
    "fault_events",
    "ordinary_oj_observations",
    "shutdown_observation",
}
PROBE_KINDS = {"measurement"} | FACT_KINDS
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROBE_CLOCK_SKEW_SECONDS = 5


class CollectorError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def require_private_directory(path: Path, label: str) -> Path:
    requested = Path(os.path.abspath(path))
    if not requested.is_dir() or requested.is_symlink():
        raise CollectorError(f"{label} must be a real directory")
    resolved = requested.resolve()
    if requested != resolved:
        raise CollectorError(f"{label} must be canonical")
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        current = current / part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise CollectorError(f"{label} has an unsafe ancestor")
        if platform.system().lower() == "linux" and (
            info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise CollectorError(f"{label} has an unsafe ancestor")
    if (
        platform.system().lower() == "linux"
        and stat.S_IMODE(resolved.stat().st_mode) & 0o077
    ):
        raise CollectorError(f"{label} must be mode 0700 or stricter")
    return resolved


def read_regular_json(path: Path, label: str, limit: int = 32 * 1024 * 1024) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CollectorError(f"cannot open {label} safely") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size <= 0
            or info.st_size > limit
            or (
                platform.system().lower() == "linux"
                and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077)
            )
        ):
            raise CollectorError(f"{label} metadata is unsafe")
        raw = b""
        while len(raw) <= limit:
            block = os.read(descriptor, min(65536, limit + 1 - len(raw)))
            if not block:
                break
            raw += block
        if len(raw) != info.st_size:
            raise CollectorError(f"{label} changed while reading")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CollectorError(f"{label} must be a JSON object")
    return value


def fsync_directory(path: Path) -> None:
    if platform.system().lower() != "linux":
        # The production command refuses non-Linux execution.  Keeping this a
        # no-op elsewhere allows deterministic unit tests of the transaction.
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_new_json(path: Path, value: dict) -> str:
    if os.path.lexists(path):
        raise CollectorError(f"output already exists: {path.name}")
    raw = canonical_json(value)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".v1-capacity-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        # Linking a fully fsynced inode publishes without replacing an entry
        # that appeared concurrently.  A check followed by os.replace() would
        # have a narrow overwrite race and would violate append-only evidence.
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise CollectorError(f"output appeared concurrently: {path.name}") from exc
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        fsync_directory(path.parent)
    return hashlib.sha256(raw).hexdigest()


def write_or_verify_json(path: Path, value: dict, label: str) -> str:
    """Finish an idempotent durable step without overwriting existing bytes."""
    raw = canonical_json(value)
    if os.path.lexists(path):
        observed = read_regular_json(path, label)
        if observed != value or path.read_bytes() != raw:
            raise CollectorError(f"existing {label} differs from the derived value")
        return hashlib.sha256(raw).hexdigest()
    return write_new_json(path, value)


def git(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectorError("Git identity check could not complete") from exc
    value = result.stdout.strip()
    if result.returncode or (arguments[:1] != ("status",) and not value):
        raise CollectorError("Git identity check failed")
    return value


def require_git_identity(source: dict) -> None:
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise CollectorError("qualification source worktree must be clean")
    if git("rev-parse", "HEAD") != source["revision"]:
        raise CollectorError("qualification source revision changed")
    if git("rev-parse", "HEAD^{tree}") != source["tree"]:
        raise CollectorError("qualification source tree changed")


def require_trusted_probe(path: Path) -> Path:
    if not path.is_absolute():
        raise CollectorError("measurement probe path must be absolute")
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    if requested != resolved:
        raise CollectorError("measurement probe path must be canonical")
    info = resolved.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise CollectorError("measurement probe metadata is unsafe")
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
            raise CollectorError("measurement probe ancestor is unsafe")
    return resolved


def probe_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_identity(value: Any) -> dict:
    identity = exact_keys(
        value,
        {"source", "components", "environment", "thresholds", "probes"},
        "capacity identity",
    )
    source = exact_keys(identity["source"], {"revision", "tree"}, "source")
    revision = require_pattern(source["revision"], HEX40, "source.revision")
    require_pattern(source["tree"], HEX40, "source.tree")
    components = exact_keys(
        identity["components"],
        {
            "orchestrator_image_digest",
            "desktop_image_id",
            "desktop_source_revision",
            "hydro_plugin_sha256",
        },
        "components",
    )
    require_pattern(components["orchestrator_image_digest"], DIGEST, "orchestrator image")
    require_pattern(components["desktop_image_id"], DIGEST, "desktop image")
    if require_pattern(components["desktop_source_revision"], HEX40, "desktop revision") != revision:
        raise CollectorError("desktop source revision differs")
    require_pattern(components["hydro_plugin_sha256"], HEX64, "Hydro plugin")
    environment = exact_keys(
        identity["environment"],
        {"profile", "instance_type", "region", "network_profile_sha256"},
        "environment",
    )
    if environment["profile"] != PROFILE:
        raise CollectorError("capacity profile differs")
    require_pattern(environment["instance_type"], SAFE_TEXT, "instance type")
    require_pattern(environment["region"], SAFE_TEXT, "region")
    require_pattern(environment["network_profile_sha256"], HEX64, "network profile")
    threshold_keys = THRESHOLD_VALUE_NAMES | {
        "thresholds_sha256",
        "capacity_margin_accepted",
    }
    thresholds = exact_keys(identity["thresholds"], threshold_keys, "thresholds")
    for key in threshold_keys - {"thresholds_sha256", "capacity_margin_accepted"}:
        require_number(thresholds[key], f"thresholds.{key}")
    require_pattern(thresholds["thresholds_sha256"], HEX64, "thresholds SHA256")
    if thresholds["thresholds_sha256"] != threshold_policy_sha256(thresholds):
        raise CollectorError("thresholds SHA256 does not match the threshold policy")
    if thresholds["capacity_margin_accepted"] is not True:
        raise CollectorError("capacity margin must be accepted before sampling")
    probes = exact_keys(identity["probes"], PROBE_KINDS, "probes")
    for kind, digest in probes.items():
        require_pattern(digest, HEX64, f"probes.{kind}")
    return identity


def build_session(
    identity: dict,
    *,
    session_id: str,
    created_at: str,
    duration_seconds: int,
    sample_interval_seconds: int,
) -> dict:
    validate_identity(identity)
    require_pattern(session_id, HEX64, "session_id")
    parse_utc(created_at, "created_at")
    if duration_seconds < 3600:
        raise CollectorError("duration must be at least 3600 seconds")
    if sample_interval_seconds < 1 or sample_interval_seconds > 60:
        raise CollectorError("sample interval must be between 1 and 60 seconds")
    return {
        "$schema": "v1-capacity-session.schema.json",
        "schema_version": 1,
        "session_id": session_id,
        "created_at": created_at,
        "source": identity["source"],
        "components": identity["components"],
        "environment": identity["environment"],
        "thresholds": identity["thresholds"],
        "probes": identity["probes"],
        "duration_seconds": duration_seconds,
        "sample_interval_seconds": sample_interval_seconds,
    }


def validate_session(value: Any) -> dict:
    session = exact_keys(
        value,
        {
            "$schema",
            "schema_version",
            "session_id",
            "created_at",
            "source",
            "components",
            "environment",
            "thresholds",
            "probes",
            "duration_seconds",
            "sample_interval_seconds",
        },
        "capacity session",
    )
    if session["$schema"] != "v1-capacity-session.schema.json" or session["schema_version"] != 1:
        raise CollectorError("capacity session identity differs")
    identity = {
        key: session[key]
        for key in ("source", "components", "environment", "thresholds", "probes")
    }
    validate_identity(identity)
    require_pattern(session["session_id"], HEX64, "session_id")
    parse_utc(session["created_at"], "created_at")
    if not isinstance(session["duration_seconds"], int) or session["duration_seconds"] < 3600:
        raise CollectorError("session duration is invalid")
    interval = session["sample_interval_seconds"]
    if not isinstance(interval, int) or isinstance(interval, bool) or not 1 <= interval <= 60:
        raise CollectorError("session sample interval is invalid")
    return session


def load_session(
    session_dir: Path, *, allow_finalized: bool = False
) -> tuple[Path, dict]:
    directory = require_private_directory(session_dir, "capacity session directory")
    session = validate_session(read_regular_json(directory / "session.json", "capacity session"))
    require_git_identity(session["source"])
    if not allow_finalized and os.path.lexists(directory / "capacity-evidence.json"):
        raise CollectorError("capacity session is already finalized")
    return directory, session


def initialize_session(
    identity: dict,
    session_dir: Path,
    *,
    duration_seconds: int,
    sample_interval_seconds: int,
    session_id: str | None = None,
    created_at: str | None = None,
) -> dict:
    identity = validate_identity(identity)
    require_git_identity(identity["source"])
    requested = Path(os.path.abspath(session_dir))
    if os.path.lexists(requested):
        raise CollectorError("capacity session directory must not already exist")
    parent = require_private_directory(requested.parent, "capacity evidence parent")
    document = build_session(
        identity,
        session_id=session_id or secrets.token_hex(32),
        created_at=created_at or utc_now(),
        duration_seconds=duration_seconds,
        sample_interval_seconds=sample_interval_seconds,
    )
    requested.mkdir(mode=0o700)
    try:
        (requested / "samples").mkdir(mode=0o700)
        (requested / "raw").mkdir(mode=0o700)
        write_new_json(requested / "session.json", document)
        for path in (requested / "samples", requested / "raw", requested, parent):
            fsync_directory(path)
    except Exception:
        # A partially initialized root-only directory is evidence of an
        # interrupted transaction.  Leave it visible; never guess-delete it.
        raise
    return document


def validate_measurement(value: Any) -> dict:
    row = exact_keys(value, {"observed_at", "metrics", "telemetry", "ordinary_oj"}, "measurement")
    observed_at = parse_utc(row["observed_at"], "measurement.observed_at")
    telemetry = exact_keys(
        row["telemetry"], {"sequence", "sha256"}, "measurement.telemetry"
    )
    if (
        isinstance(telemetry["sequence"], bool)
        or not isinstance(telemetry["sequence"], int)
        or telemetry["sequence"] < 1
    ):
        raise CollectorError("measurement telemetry sequence is invalid")
    require_pattern(telemetry["sha256"], HEX64, "measurement telemetry SHA256")
    ordinary = exact_keys(
        row["ordinary_oj"], {
            "schema_version", "qualification_marker", "sequence", "observed_at", "homepage_status", "login_status",
            "prep_health_ok", "prep_database_ok", "ordinary_oj_errors", "ordinary_oj_restarts",
            "ordinary_oj_pid_changes", "credential_leaks", "result_leaks",
            "pm2_fingerprint_sha256", "sha256",
        }, "measurement.ordinary_oj",
    )
    ordinary_at = parse_utc(ordinary["observed_at"], "measurement.ordinary_oj.observed_at")
    if abs((observed_at - ordinary_at).total_seconds()) > 60:
        raise CollectorError("ordinary OJ observation is not bound to the measurement")
    if ordinary["schema_version"] != 1 or isinstance(ordinary["sequence"], bool) or \
            not isinstance(ordinary["sequence"], int) or ordinary["sequence"] < 1:
        raise CollectorError("ordinary OJ observation identity is invalid")
    if not isinstance(ordinary["qualification_marker"], str) or not re.fullmatch(
            r"NOI-V1-QUAL-[A-Z0-9]{16,64}", ordinary["qualification_marker"]
    ):
        raise CollectorError("ordinary OJ qualification marker is invalid")
    if ordinary["homepage_status"] != 200 or ordinary["login_status"] != 200 or \
            ordinary["prep_health_ok"] is not True or ordinary["prep_database_ok"] is not True:
        raise CollectorError("ordinary OJ observation is unhealthy")
    for key in ("ordinary_oj_errors", "ordinary_oj_restarts", "ordinary_oj_pid_changes",
                "credential_leaks", "result_leaks"):
        if ordinary[key] != 0:
            raise CollectorError(f"ordinary OJ observation {key} is non-zero")
    require_pattern(ordinary["pm2_fingerprint_sha256"], HEX64, "ordinary OJ PM2 fingerprint")
    require_pattern(ordinary["sha256"], HEX64, "ordinary OJ observation SHA256")
    metrics = exact_keys(row["metrics"], METRIC_NAMES, "measurement.metrics")
    for key, value in metrics.items():
        number = require_number(value, f"measurement.metrics.{key}")
        if key in {
            "host_cpu_peak_percent",
            "host_memory_peak_percent",
            "packet_loss_percent",
        } and number > 100:
            raise CollectorError(f"measurement.metrics.{key} exceeds 100")
        if key == "websocket_reconnects" and int(number) != number:
            raise CollectorError("websocket reconnects must be an integer")
    if metrics["container_memory_peak_bytes"] <= 0:
        raise CollectorError("container memory must be positive")
    if (
        metrics["rtt_p95_ms"] <= 0
        or metrics["key_to_frame_p50_ms"] <= 0
        or metrics["key_to_frame_p95_ms"] < metrics["key_to_frame_p50_ms"]
    ):
        raise CollectorError("latency measurement is invalid")
    return row


def sample_files(directory: Path) -> list[Path]:
    files = sorted(directory.glob("*.json"))
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise CollectorError("sample directory contains an unsafe entry")
    return files


def record_sample(
    session_dir: Path, measurement: dict, *, trusted_probe_sha256: str | None = None
) -> dict:
    directory, session = load_session(session_dir)
    if trusted_probe_sha256 is not None and \
            trusted_probe_sha256 != session["probes"]["measurement"]:
        raise CollectorError("measurement probe SHA256 differs from the frozen session")
    measurement = validate_measurement(measurement)
    samples_dir = require_private_directory(directory / "samples", "sample directory")
    files = sample_files(samples_dir)
    observed = parse_utc(measurement["observed_at"], "measurement.observed_at")
    if files:
        previous = read_regular_json(files[-1], "previous capacity sample")
        previous_at = parse_utc(previous["observed_at"], "previous sample observed_at")
        delta = (observed - previous_at).total_seconds()
        if delta <= 0:
            raise CollectorError("capacity sample timestamps must increase")
        if delta > session["sample_interval_seconds"] + 2:
            raise CollectorError("capacity sample cadence gap exceeds tolerance")
        previous_telemetry = exact_keys(
            previous.get("telemetry"), {"sequence", "sha256"}, "previous telemetry"
        )
        if (
            measurement["telemetry"]["sequence"] <= previous_telemetry["sequence"]
            or measurement["telemetry"]["sha256"] == previous_telemetry["sha256"]
        ):
            raise CollectorError("browser telemetry was replayed or moved backwards")
        previous_ordinary = previous.get("ordinary_oj")
        if not isinstance(previous_ordinary, dict) or \
                measurement["ordinary_oj"]["sequence"] <= previous_ordinary.get("sequence", 0) or \
                measurement["ordinary_oj"]["sha256"] == previous_ordinary.get("sha256"):
            raise CollectorError("ordinary OJ telemetry was replayed or moved backwards")
        if measurement["ordinary_oj"]["pm2_fingerprint_sha256"] != \
                previous_ordinary.get("pm2_fingerprint_sha256"):
            raise CollectorError("ordinary OJ PM2 fingerprint changed during sampling")
    elif observed < parse_utc(session["created_at"], "session.created_at"):
        raise CollectorError("first capacity sample predates the session")
    sequence = len(files) + 1
    fact = {
        "schema_version": 1,
        "kind": "capacity_sample",
        "session_id": session["session_id"],
        "sequence": sequence,
        "observed_at": measurement["observed_at"],
        "metrics": measurement["metrics"],
        "telemetry": measurement["telemetry"],
        "ordinary_oj": measurement["ordinary_oj"],
        "collector": {
            "mode": "trusted_probe" if trusted_probe_sha256 else "manual_input",
            "probe_sha256": trusted_probe_sha256,
        },
    }
    name = f"{sequence:06d}-{hashlib.sha256(measurement['observed_at'].encode()).hexdigest()[:12]}.json"
    digest = write_new_json(samples_dir / name, fact)
    return {"sequence": sequence, "sample_sha256": digest}


def run_json_probe(probe: Path, label: str) -> tuple[dict, str]:
    binary = require_trusted_probe(probe)
    digest = probe_sha256(binary)
    started = datetime.now(timezone.utc)
    try:
        result = subprocess.run(
            [str(binary)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "HOME": "/root",
                "USER": "root",
                "LOGNAME": "root",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectorError(f"{label} probe could not complete") from exc
    if result.returncode or result.stderr or len(result.stdout) > 1024 * 1024:
        raise CollectorError(f"{label} probe failed or wrote stderr")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CollectorError(f"{label} probe did not return strict JSON") from exc
    if not isinstance(value, dict):
        raise CollectorError(f"{label} probe must return a JSON object")
    finished = datetime.now(timezone.utc)
    observed = parse_utc(value.get("observed_at"), f"{label}.observed_at")
    if (
        observed < started - timedelta(seconds=PROBE_CLOCK_SKEW_SECONDS)
        or observed > finished + timedelta(seconds=PROBE_CLOCK_SKEW_SECONDS)
    ):
        raise CollectorError(f"{label} probe timestamp is not bound to this invocation")
    return value, digest


def run_probe(probe: Path) -> tuple[dict, str]:
    value, digest = run_json_probe(probe, "measurement")
    return validate_measurement(value), digest


def run_sampling_loop(
    session_dir: Path,
    probe: Path,
    *,
    sleep_function=None,
) -> dict:
    """Run the complete fixed-cadence window; abort on any missed sample."""
    import time

    sleeper = sleep_function or time.sleep
    directory, session = load_session(session_dir)
    existing = sample_files(directory / "samples")
    if existing:
        raise CollectorError("run requires a fresh session with no samples")
    planned_samples = session["duration_seconds"] // session["sample_interval_seconds"] + 1
    monotonic_start = time.monotonic()
    for index in range(planned_samples):
        deadline = monotonic_start + index * session["sample_interval_seconds"]
        remaining = deadline - time.monotonic()
        if remaining > 0:
            sleeper(remaining)
        elif remaining < -2:
            raise CollectorError("sampling loop missed its cadence deadline")
        measurement, digest = run_probe(probe)
        record_sample(session_dir, measurement, trusted_probe_sha256=digest)
    return {"planned_samples": planned_samples, "status": "sampled"}


def fact_keys(kind: str) -> set[str]:
    return {
        "seat_inventory": {
            "formal_container_ids",
            "spare_container_ids",
            "verified_container_ids",
            "unexpected_restart_events",
            "planned_restart_events",
            "planned_restart_recoveries",
            "cross_seat_access_failures",
        },
        "workload_events": {
            "login_successes",
            "material_open_successes",
            "compile_successes",
            "submission_successes",
            "failed_submissions",
            "collection_successes",
            "failed_collections",
            "final_source_mismatches",
        },
        "fault_events": {
            "spare_takeovers",
            "spare_takeovers_recovered",
            "planned_restart_events",
            "planned_restart_recoveries",
            "controller_network_interruptions",
            "controller_network_recoveries",
        },
        "ordinary_oj_observations": {
            "ordinary_oj_errors",
            "ordinary_oj_restarts",
            "ordinary_oj_pid_changes",
            "credential_leaks",
            "result_leaks",
        },
        "shutdown_observation": {
            "active_seats",
            "managed_rules",
            "conflict_rules",
            "cloud_state",
            "delivery_queues",
            "notification_queues",
        },
    }[kind]


def validate_fact_payload(kind: str, value: Any) -> dict:
    row = exact_keys(value, {"observed_at"} | fact_keys(kind), f"{kind} payload")
    parse_utc(row["observed_at"], f"{kind}.observed_at")
    if kind == "seat_inventory":
        for key in ("formal_container_ids", "spare_container_ids", "verified_container_ids"):
            values = row[key]
            if not isinstance(values, list):
                raise CollectorError(f"{kind}.{key} must be an array")
            for item in values:
                require_pattern(item, HEX64, f"{kind}.{key}")
            if len(values) != len(set(values)):
                raise CollectorError(f"{kind}.{key} contains duplicates")
        for key in (
            "unexpected_restart_events", "planned_restart_events",
            "planned_restart_recoveries", "cross_seat_access_failures",
        ):
            if isinstance(row[key], bool) or not isinstance(row[key], int) or row[key] < 0:
                raise CollectorError(f"{kind}.{key} must be non-negative")
    elif kind == "shutdown_observation":
        for key, item in row.items():
            if key == "observed_at":
                continue
            if key == "cloud_state":
                if item not in {"RUNNING", "STOPPING", "STOPPED"}:
                    raise CollectorError("shutdown cloud state is invalid")
            elif isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise CollectorError(f"{kind}.{key} must be non-negative")
    else:
        for key, item in row.items():
            if key == "observed_at":
                continue
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise CollectorError(f"{kind}.{key} must be non-negative")
    return row


def record_fact(
    session_dir: Path,
    kind: str,
    payload: dict,
    *,
    trusted_probe_sha256: str | None = None,
) -> dict:
    directory, session = load_session(session_dir)
    if kind not in FACT_KINDS:
        raise CollectorError("capacity fact kind is invalid")
    payload = validate_fact_payload(kind, payload)
    if trusted_probe_sha256 is not None and trusted_probe_sha256 != session["probes"][kind]:
        raise CollectorError(f"{kind} probe SHA256 differs from the frozen session")
    raw_dir = require_private_directory(directory / "raw", "raw evidence directory")
    fact = {
        "schema_version": 1,
        "kind": kind,
        "session_id": session["session_id"],
        "collector": {
            "mode": "trusted_probe" if trusted_probe_sha256 else "manual_input",
            "probe_sha256": trusted_probe_sha256,
        },
        **payload,
    }
    digest = write_new_json(raw_dir / f"{kind}.json", fact)
    return {"fact": kind, "fact_sha256": digest}


def load_samples(directory: Path, session: dict) -> list[dict]:
    rows: list[dict] = []
    for index, path in enumerate(sample_files(directory), start=1):
        row = exact_keys(
            read_regular_json(path, f"capacity sample {index}"),
            {"schema_version", "kind", "session_id", "sequence", "observed_at", "metrics", "telemetry", "ordinary_oj", "collector"},
            f"capacity sample {index}",
        )
        if (
            row["schema_version"] != 1
            or row["kind"] != "capacity_sample"
            or row["session_id"] != session["session_id"]
            or row["sequence"] != index
        ):
            raise CollectorError("capacity sample chain differs")
        collector = exact_keys(
            row["collector"], {"mode", "probe_sha256"}, "sample collector"
        )
        if collector["mode"] != "trusted_probe":
            raise CollectorError("capacity qualification samples require trusted probes")
        require_pattern(collector["probe_sha256"], HEX64, "sample probe SHA256")
        validate_measurement(
            {
                "observed_at": row["observed_at"],
                "metrics": row["metrics"],
                "telemetry": row["telemetry"],
                "ordinary_oj": row["ordinary_oj"],
            }
        )
        rows.append(row)
    return rows


def derive_metrics(samples: list[dict]) -> dict:
    metrics = {
        key: max(float(row["metrics"][key]) for row in samples)
        for key in METRIC_NAMES - {"websocket_reconnects"}
    }
    metrics["websocket_reconnects"] = sum(
        int(row["metrics"]["websocket_reconnects"]) for row in samples
    )
    return metrics


def artifact_row(path: Path, name: str, session_dir: Path) -> dict:
    raw = path.read_bytes()
    return {
        "name": name,
        "reference": path.relative_to(session_dir).as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def finalize_session(session_dir: Path) -> dict:
    directory, session = load_session(session_dir, allow_finalized=True)
    final_path = directory / "capacity-evidence.json"
    if os.path.lexists(final_path):
        evidence = read_regular_json(final_path, "capacity evidence")
        try:
            validate_capacity_evidence(
                evidence,
                expected_revision=session["source"]["revision"],
                expected_tree=session["source"]["tree"],
                expected_components=session["components"],
                artifact_root=directory,
            )
        except EvidenceError as exc:
            raise CollectorError(str(exc)) from exc
        return {
            "evidence_sha256": hashlib.sha256(final_path.read_bytes()).hexdigest(),
            "sample_count": evidence["window"]["sample_count"],
        }
    samples_dir = require_private_directory(directory / "samples", "sample directory")
    raw_dir = require_private_directory(directory / "raw", "raw evidence directory")
    samples = load_samples(samples_dir, session)
    if not samples:
        raise CollectorError("capacity session has no samples")
    started = parse_utc(samples[0]["observed_at"], "first sample")
    ended = parse_utc(samples[-1]["observed_at"], "last sample")
    duration = int((ended - started).total_seconds())
    if duration < session["duration_seconds"]:
        raise CollectorError("capacity session has not reached its planned duration")
    minimum_samples = duration // session["sample_interval_seconds"] + 1
    if len(samples) < minimum_samples:
        raise CollectorError("capacity sample count is too sparse")
    expected_fact_names = {f"{kind}.json" for kind in FACT_KINDS}
    observed_fact_names = {
        path.name for path in raw_dir.iterdir() if path.name != "sample_series.json"
    }
    if observed_fact_names != expected_fact_names:
        raise CollectorError("capacity summary fact file set differs")
    facts = {
        kind: read_regular_json(raw_dir / f"{kind}.json", kind)
        for kind in FACT_KINDS
    }
    for kind, fact in facts.items():
        if (
            fact.get("schema_version") != 1
            or fact.get("kind") != kind
            or fact.get("session_id") != session["session_id"]
        ):
            raise CollectorError(f"{kind} fact identity differs")
        collector = exact_keys(
            fact.get("collector"), {"mode", "probe_sha256"}, f"{kind} collector"
        )
        if collector["mode"] != "trusted_probe":
            raise CollectorError(f"{kind} qualification fact requires a trusted probe")
        require_pattern(collector["probe_sha256"], HEX64, f"{kind} probe SHA256")
        validate_fact_payload(
            kind,
            {"observed_at": fact["observed_at"]}
            | {key: fact[key] for key in fact_keys(kind)},
        )
        fact_at = parse_utc(fact["observed_at"], f"{kind}.observed_at")
        closeout = (fact_at - ended).total_seconds()
        maximum = (
            PRE_COLLECTION_FACT_SECONDS
            if kind in {"seat_inventory", "fault_events"}
            else POST_SHUTDOWN_FACT_SECONDS
        )
        if fact_at < started or closeout < 0 or closeout > maximum:
            raise CollectorError(
                f"{kind} must be observed within {maximum} seconds after the sample window"
            )
    series = {
        "schema_version": 1,
        "kind": "sample_series",
        "session_id": session["session_id"],
        "source": session["source"],
        "components": session["components"],
        "environment": session["environment"],
        "thresholds": session["thresholds"],
        "started_at": samples[0]["observed_at"],
        "ended_at": samples[-1]["observed_at"],
        "sample_interval_seconds": session["sample_interval_seconds"],
        "measurement_probe_sha256": samples[0]["collector"]["probe_sha256"],
        "samples": [
            {
                "observed_at": row["observed_at"],
                "telemetry": row["telemetry"],
                "ordinary_oj": row["ordinary_oj"],
                **row["metrics"],
            }
            for row in samples
        ],
    }
    if any(
        row["collector"]["probe_sha256"] != series["measurement_probe_sha256"]
        for row in samples
    ):
        raise CollectorError("measurement probe changed during the capacity window")
    telemetry_sequences = [row["telemetry"]["sequence"] for row in samples]
    telemetry_hashes = [row["telemetry"]["sha256"] for row in samples]
    if any(
        current <= previous
        for previous, current in zip(telemetry_sequences, telemetry_sequences[1:])
    ) or len(set(telemetry_hashes)) != len(telemetry_hashes):
        raise CollectorError("browser telemetry was replayed or moved backwards")
    ordinary_sequences = [row["ordinary_oj"]["sequence"] for row in samples]
    ordinary_hashes = [row["ordinary_oj"]["sha256"] for row in samples]
    ordinary_fingerprints = {
        row["ordinary_oj"]["pm2_fingerprint_sha256"] for row in samples
    }
    ordinary_markers = {row["ordinary_oj"]["qualification_marker"] for row in samples}
    if any(current <= previous for previous, current in zip(
            ordinary_sequences, ordinary_sequences[1:])) or \
            len(set(ordinary_hashes)) != len(ordinary_hashes) or len(ordinary_fingerprints) != 1 or \
            len(ordinary_markers) != 1:
        raise CollectorError("ordinary OJ telemetry replayed, moved backwards, or changed PM2 identity")
    isolation = facts["ordinary_oj_observations"]
    if any(isolation[key] != 0 for key in fact_keys("ordinary_oj_observations")):
        raise CollectorError("ordinary OJ terminal observation is not clean")
    series_path = raw_dir / "sample_series.json"
    write_or_verify_json(series_path, series, "sample series")
    seat = facts["seat_inventory"]
    formal_ids = seat["formal_container_ids"]
    spare_ids = seat["spare_container_ids"]
    verified_ids = seat["verified_container_ids"]
    unique_ids = set(formal_ids) | set(spare_ids)
    if seat["planned_restart_events"] != 1 or \
            seat["planned_restart_recoveries"] != 1 or \
            facts["fault_events"]["planned_restart_events"] != seat["planned_restart_events"] or \
            facts["fault_events"]["planned_restart_recoveries"] != seat["planned_restart_recoveries"]:
        raise CollectorError("planned restart fault summary is not derived from seat lifecycle evidence")
    evidence = {
        "$schema": "v1-capacity-evidence.schema.json",
        "schema_version": 1,
        "status": "passed",
        "session_id": session["session_id"],
        "source": session["source"],
        "components": session["components"],
        "environment": session["environment"],
        "probes": session["probes"],
        "window": {
            "started_at": samples[0]["observed_at"],
            "ended_at": samples[-1]["observed_at"],
            "duration_seconds": duration,
            "sample_interval_seconds": session["sample_interval_seconds"],
            "sample_count": len(samples),
        },
        "seats": {
            "formal": len(formal_ids),
            "spare": len(spare_ids),
            "verified": len(verified_ids),
            "unique_container_ids": len(unique_ids),
            "unexpected_restart_events": seat["unexpected_restart_events"],
            "cross_seat_access_failures": seat["cross_seat_access_failures"],
        },
        "workload": {key: facts["workload_events"][key] for key in fact_keys("workload_events")},
        "faults": {key: facts["fault_events"][key] for key in fact_keys("fault_events")},
        "isolation": {
            key: facts["ordinary_oj_observations"][key]
            for key in fact_keys("ordinary_oj_observations")
        },
        "shutdown": {
            key: facts["shutdown_observation"][key]
            for key in fact_keys("shutdown_observation")
        },
        "metrics": derive_metrics(samples),
        "thresholds": session["thresholds"],
        "artifacts": [],
    }
    evidence["artifacts"] = [
        artifact_row(raw_dir / f"{name}.json", name, directory)
        for name in sorted(ARTIFACT_NAMES)
    ]
    try:
        validate_capacity_evidence(
            evidence,
            expected_revision=session["source"]["revision"],
            expected_tree=session["source"]["tree"],
            expected_components=session["components"],
            artifact_root=directory,
        )
    except EvidenceError as exc:
        raise CollectorError(str(exc)) from exc
    digest = write_or_verify_json(
        final_path, evidence, "capacity evidence"
    )
    return {"evidence_sha256": digest, "sample_count": len(samples)}


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--identity", type=Path, required=True)
    init.add_argument("--session-dir", type=Path, required=True)
    init.add_argument("--duration-seconds", type=int, default=3600)
    init.add_argument("--sample-interval-seconds", type=int, default=10)
    sample = subparsers.add_parser("sample")
    sample.add_argument("--session-dir", type=Path, required=True)
    sample.add_argument("--measurement", type=Path, required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--session-dir", type=Path, required=True)
    run.add_argument("--probe", type=Path, required=True)
    threshold = subparsers.add_parser("threshold-sha256")
    threshold.add_argument("--input", type=Path, required=True)
    fact = subparsers.add_parser("fact")
    fact.add_argument("--session-dir", type=Path, required=True)
    fact.add_argument("--kind", choices=sorted(FACT_KINDS), required=True)
    fact_source = fact.add_mutually_exclusive_group(required=True)
    fact_source.add_argument("--input", type=Path)
    fact_source.add_argument("--probe", type=Path)
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--session-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise CollectorError("capacity evidence collection requires Linux root")
        if args.command == "threshold-sha256":
            thresholds = read_regular_json(args.input, "threshold policy")
            if set(thresholds) != THRESHOLD_VALUE_NAMES:
                raise CollectorError("threshold policy field set differs")
            output = {
                "status": "derived",
                "thresholds_sha256": threshold_policy_sha256(thresholds),
            }
        elif args.command == "init":
            identity = read_regular_json(args.identity, "capacity identity")
            result = initialize_session(
                identity,
                args.session_dir,
                duration_seconds=args.duration_seconds,
                sample_interval_seconds=args.sample_interval_seconds,
            )
            output = {
                "session_id": result["session_id"],
                "status": "initialized",
            }
        elif args.command == "sample":
            output = {
                **record_sample(
                    args.session_dir,
                    read_regular_json(args.measurement, "capacity measurement"),
                ),
                "status": "sampled",
            }
        elif args.command == "run":
            output = run_sampling_loop(args.session_dir, args.probe)
        elif args.command == "fact":
            probe_digest = None
            if args.input is not None:
                payload = read_regular_json(args.input, "capacity fact input")
            else:
                payload, probe_digest = run_json_probe(args.probe, args.kind)
            output = {
                **record_fact(
                    args.session_dir,
                    args.kind,
                    payload,
                    trusted_probe_sha256=probe_digest,
                ),
                "status": "recorded",
            }
        else:
            output = {**finalize_session(args.session_dir), "status": "passed"}
        print(json.dumps(output, sort_keys=True))
        return 0
    except (CollectorError, EvidenceError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
