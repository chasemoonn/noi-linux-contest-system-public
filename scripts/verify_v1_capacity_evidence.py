#!/usr/bin/env python3
"""Validate the fixed 15+2, one-hour NOI Linux V1 capacity evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


PROFILE = "aliyun-hydro5-pm2-direct-v1"
HEX40 = re.compile(r"^[a-f0-9]{40}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
SAFE_TEXT = re.compile(r"^[A-Za-z0-9._:/+ -]{1,160}$")
ARTIFACT_NAMES = {
    "sample_series",
    "seat_inventory",
    "workload_events",
    "fault_events",
    "ordinary_oj_observations",
    "shutdown_observation",
}
PROBE_KINDS = {"measurement"} | (ARTIFACT_NAMES - {"sample_series"})
THRESHOLD_VALUE_NAMES = {
    "host_cpu_peak_percent_max",
    "host_memory_peak_percent_max",
    "container_memory_peak_bytes_max",
    "egress_peak_mbps_max",
    "rtt_p95_ms_max",
    "packet_loss_percent_max",
    "websocket_reconnects_max",
    "key_to_frame_p95_ms_max",
}
PRE_COLLECTION_FACT_SECONDS = 300
POST_SHUTDOWN_FACT_SECONDS = 2700


class EvidenceError(ValueError):
    pass


def exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise EvidenceError(
            f"{label} keys differ: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )
    return value


def require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvidenceError(f"{label} must be an integer >= {minimum}")
    return value


def require_number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise EvidenceError(f"{label} must be >= {minimum}")
    return number


def require_pattern(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise EvidenceError(f"{label} has an invalid value")
    return value


def require_true(value: Any, label: str) -> None:
    if value is not True:
        raise EvidenceError(f"{label} must be true")


def parse_utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvidenceError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise EvidenceError(f"{label} must use UTC")
    return parsed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def threshold_policy_sha256(thresholds: dict[str, Any]) -> str:
    values = {key: thresholds[key] for key in sorted(THRESHOLD_VALUE_NAMES)}
    raw = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def safe_reference(value: Any, label: str) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise EvidenceError(f"{label} must be a safe relative POSIX path")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise EvidenceError(f"{label} must be a safe relative POSIX path")
    return value


def load_artifact_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must contain a JSON object")
    return value


def require_capacity_fact_header(
    value: dict[str, Any], *, kind: str, session_id: str, extra_keys: set[str]
) -> dict[str, Any]:
    row = exact_keys(
        value,
        {"schema_version", "kind", "session_id"} | extra_keys,
        f"{kind} artifact",
    )
    if row["schema_version"] != 1 or row["kind"] != kind:
        raise EvidenceError(f"{kind} artifact identity differs")
    if row["session_id"] != session_id:
        raise EvidenceError(f"{kind} artifact session differs")
    return row


def validate_sample_series_artifact(
    value: dict[str, Any], evidence: dict[str, Any]
) -> None:
    row = require_capacity_fact_header(
        value,
        kind="sample_series",
        session_id=evidence["session_id"],
        extra_keys={
            "source",
            "components",
            "environment",
            "thresholds",
            "started_at",
            "ended_at",
            "sample_interval_seconds",
            "measurement_probe_sha256",
            "samples",
        },
    )
    require_pattern(
        row["measurement_probe_sha256"], HEX64, "measurement probe SHA256"
    )
    if row["measurement_probe_sha256"] != evidence["probes"]["measurement"]:
        raise EvidenceError("measurement probe SHA256 differs from the frozen session")
    for key in ("source", "components", "environment", "thresholds"):
        if row[key] != evidence[key]:
            raise EvidenceError(f"sample_series.{key} differs from capacity evidence")
    window = evidence["window"]
    if (
        row["started_at"] != window["started_at"]
        or row["ended_at"] != window["ended_at"]
        or row["sample_interval_seconds"] != window["sample_interval_seconds"]
    ):
        raise EvidenceError("sample series window differs from capacity evidence")
    samples = row["samples"]
    if not isinstance(samples, list) or len(samples) != window["sample_count"]:
        raise EvidenceError("sample series count differs from capacity evidence")
    metric_keys = set(evidence["metrics"])
    expected_sample_keys = {"observed_at", "telemetry", "ordinary_oj"} | metric_keys
    previous: datetime | None = None
    previous_telemetry_sequence: int | None = None
    telemetry_hashes: set[str] = set()
    previous_ordinary_sequence: int | None = None
    ordinary_hashes: set[str] = set()
    ordinary_fingerprints: set[str] = set()
    ordinary_markers: set[str] = set()
    observed_metrics: dict[str, list[float]] = {key: [] for key in metric_keys}
    reconnect_total = 0
    for index, sample_value in enumerate(samples):
        sample = exact_keys(sample_value, expected_sample_keys, f"samples[{index}]")
        observed_at = parse_utc(sample["observed_at"], f"samples[{index}].observed_at")
        if previous is not None:
            delta = (observed_at - previous).total_seconds()
            if delta <= 0 or delta > window["sample_interval_seconds"] + 2:
                raise EvidenceError("sample timestamps are not strictly ordered at the declared cadence")
        previous = observed_at
        telemetry = exact_keys(
            sample["telemetry"], {"sequence", "sha256"}, f"samples[{index}].telemetry"
        )
        telemetry_sequence = require_int(
            telemetry["sequence"], f"samples[{index}].telemetry.sequence", minimum=1
        )
        telemetry_hash = require_pattern(
            telemetry["sha256"], HEX64, f"samples[{index}].telemetry.sha256"
        )
        if (
            previous_telemetry_sequence is not None
            and telemetry_sequence <= previous_telemetry_sequence
        ) or telemetry_hash in telemetry_hashes:
            raise EvidenceError("browser telemetry was replayed or moved backwards")
        previous_telemetry_sequence = telemetry_sequence
        telemetry_hashes.add(telemetry_hash)
        ordinary = exact_keys(
            sample["ordinary_oj"], {
                "schema_version", "qualification_marker", "sequence", "observed_at", "homepage_status", "login_status",
                "prep_health_ok", "prep_database_ok", "ordinary_oj_errors", "ordinary_oj_restarts",
                "ordinary_oj_pid_changes", "credential_leaks", "result_leaks",
                "pm2_fingerprint_sha256", "sha256",
            }, f"samples[{index}].ordinary_oj",
        )
        ordinary_at = parse_utc(ordinary["observed_at"], f"samples[{index}].ordinary_oj.observed_at")
        if abs((observed_at - ordinary_at).total_seconds()) > 60 or ordinary["schema_version"] != 1:
            raise EvidenceError("ordinary OJ observation is not bound to its sample")
        ordinary_sequence = require_int(
            ordinary["sequence"], f"samples[{index}].ordinary_oj.sequence", minimum=1
        )
        ordinary_hash = require_pattern(
            ordinary["sha256"], HEX64, f"samples[{index}].ordinary_oj.sha256"
        )
        if (previous_ordinary_sequence is not None and ordinary_sequence <= previous_ordinary_sequence) \
                or ordinary_hash in ordinary_hashes:
            raise EvidenceError("ordinary OJ telemetry was replayed or moved backwards")
        previous_ordinary_sequence = ordinary_sequence
        ordinary_hashes.add(ordinary_hash)
        ordinary_fingerprints.add(require_pattern(
            ordinary["pm2_fingerprint_sha256"], HEX64,
            f"samples[{index}].ordinary_oj.pm2_fingerprint_sha256",
        ))
        ordinary_markers.add(require_pattern(
            ordinary["qualification_marker"], re.compile(r"NOI-V1-QUAL-[A-Z0-9]{16,64}"),
            f"samples[{index}].ordinary_oj.qualification_marker",
        ))
        if ordinary["homepage_status"] != 200 or ordinary["login_status"] != 200 or \
                ordinary["prep_health_ok"] is not True or ordinary["prep_database_ok"] is not True:
            raise EvidenceError("ordinary OJ sample is unhealthy")
        for key in ("ordinary_oj_errors", "ordinary_oj_restarts", "ordinary_oj_pid_changes",
                    "credential_leaks", "result_leaks"):
            if ordinary[key] != 0:
                raise EvidenceError(f"ordinary OJ sample {key} is non-zero")
        for key in metric_keys:
            number = require_number(sample[key], f"samples[{index}].{key}")
            observed_metrics[key].append(number)
        reconnect = sample["websocket_reconnects"]
        if int(reconnect) != reconnect:
            raise EvidenceError("sample websocket reconnects must be integers")
        reconnect_total += int(reconnect)
    if not samples:
        raise EvidenceError("sample series is empty")
    if len(ordinary_fingerprints) != 1 or len(ordinary_markers) != 1:
        raise EvidenceError("ordinary OJ PM2 fingerprint or qualification marker changed during the capacity window")
    if samples[0]["observed_at"] != window["started_at"] or samples[-1]["observed_at"] != window["ended_at"]:
        raise EvidenceError("sample series must include both window boundaries")
    derived = {
        key: max(values)
        for key, values in observed_metrics.items()
        if key != "websocket_reconnects"
    }
    derived["websocket_reconnects"] = reconnect_total
    for key, value in evidence["metrics"].items():
        if float(value) != float(derived[key]):
            raise EvidenceError(f"metrics.{key} is not derived from the sample series")


def validate_summary_artifact(
    value: dict[str, Any], evidence: dict[str, Any], *, kind: str, section: str
) -> None:
    expected = set(evidence[section])
    row = require_capacity_fact_header(
        value,
        kind=kind,
        session_id=evidence["session_id"],
        extra_keys={"observed_at", "collector"} | expected,
    )
    collector = exact_keys(row["collector"], {"mode", "probe_sha256"}, f"{kind}.collector")
    if collector["mode"] != "trusted_probe":
        raise EvidenceError(f"{kind} must come from a trusted probe")
    probe_sha = require_pattern(collector["probe_sha256"], HEX64, f"{kind} probe SHA256")
    if probe_sha != evidence["probes"][kind]:
        raise EvidenceError(f"{kind} probe SHA256 differs from the frozen session")
    parse_utc(row["observed_at"], f"{kind}.observed_at")
    for key in expected:
        if row[key] != evidence[section][key]:
            raise EvidenceError(f"{kind}.{key} differs from capacity evidence")


def validate_seat_inventory_artifact(
    value: dict[str, Any], evidence: dict[str, Any]
) -> None:
    seats = evidence["seats"]
    row = require_capacity_fact_header(
        value,
        kind="seat_inventory",
        session_id=evidence["session_id"],
        extra_keys={
            "observed_at",
            "collector",
            "formal_container_ids",
            "spare_container_ids",
            "verified_container_ids",
            "unexpected_restart_events",
            "planned_restart_events",
            "planned_restart_recoveries",
            "cross_seat_access_failures",
        },
    )
    collector = exact_keys(
        row["collector"], {"mode", "probe_sha256"}, "seat_inventory.collector"
    )
    if collector["mode"] != "trusted_probe":
        raise EvidenceError("seat_inventory must come from a trusted probe")
    require_pattern(collector["probe_sha256"], HEX64, "seat inventory probe SHA256")
    if collector["probe_sha256"] != evidence["probes"]["seat_inventory"]:
        raise EvidenceError("seat inventory probe SHA256 differs from the frozen session")
    parse_utc(row["observed_at"], "seat_inventory.observed_at")
    groups: dict[str, list[str]] = {}
    for key in ("formal_container_ids", "spare_container_ids", "verified_container_ids"):
        values = row[key]
        if not isinstance(values, list):
            raise EvidenceError(f"seat_inventory.{key} must be an array")
        groups[key] = [require_pattern(value, HEX64, f"seat_inventory.{key}") for value in values]
        if len(groups[key]) != len(set(groups[key])):
            raise EvidenceError(f"seat_inventory.{key} contains duplicates")
    formal = groups["formal_container_ids"]
    spare = groups["spare_container_ids"]
    verified = groups["verified_container_ids"]
    if len(formal) != seats["formal"] or len(spare) != seats["spare"]:
        raise EvidenceError("seat inventory formal/spare counts differ")
    all_ids = set(formal) | set(spare)
    if set(formal) & set(spare) or len(all_ids) != seats["unique_container_ids"]:
        raise EvidenceError("seat inventory identities overlap or differ")
    if set(verified) != all_ids or len(verified) != seats["verified"]:
        raise EvidenceError("seat inventory verified set differs")
    for key in ("unexpected_restart_events", "cross_seat_access_failures"):
        if row[key] != seats[key]:
            raise EvidenceError(f"seat_inventory.{key} differs")
    for key in ("planned_restart_events", "planned_restart_recoveries"):
        if row[key] != evidence["faults"][key]:
            raise EvidenceError(f"seat_inventory.{key} differs from fault evidence")
    if row["planned_restart_events"] != 1 or row["planned_restart_recoveries"] != 1:
        raise EvidenceError("seat inventory must prove exactly one recovered planned restart")


def validate_private_artifacts(
    evidence: dict[str, Any], paths: dict[str, Path]
) -> None:
    values = {name: load_artifact_json(path, name) for name, path in paths.items()}
    validate_sample_series_artifact(values["sample_series"], evidence)
    validate_seat_inventory_artifact(values["seat_inventory"], evidence)
    validate_summary_artifact(
        values["workload_events"], evidence, kind="workload_events", section="workload"
    )
    validate_summary_artifact(
        values["fault_events"], evidence, kind="fault_events", section="faults"
    )
    validate_summary_artifact(
        values["ordinary_oj_observations"],
        evidence,
        kind="ordinary_oj_observations",
        section="isolation",
    )
    validate_summary_artifact(
        values["shutdown_observation"],
        evidence,
        kind="shutdown_observation",
        section="shutdown",
    )
    started = parse_utc(evidence["window"]["started_at"], "window.started_at")
    ended = parse_utc(evidence["window"]["ended_at"], "window.ended_at")
    for kind in ARTIFACT_NAMES - {"sample_series"}:
        observed_at = parse_utc(values[kind]["observed_at"], f"{kind}.observed_at")
        delta = (observed_at - ended).total_seconds()
        maximum = (
            PRE_COLLECTION_FACT_SECONDS
            if kind in {"seat_inventory", "fault_events"}
            else POST_SHUTDOWN_FACT_SECONDS
        )
        if observed_at < started or delta < 0 or delta > maximum:
            raise EvidenceError(
                f"{kind} must be observed within {maximum} seconds after the sample window"
            )


def capacity_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    seats = evidence["seats"]
    window = evidence["window"]
    workload = evidence["workload"]
    faults = evidence["faults"]
    return {
        "formal_seats": seats["formal"],
        "spare_seats": seats["spare"],
        "duration_seconds": window["duration_seconds"],
        "unexpected_seat_restarts": seats["unexpected_restart_events"],
        "failed_submissions": workload["failed_submissions"],
        "failed_collections": workload["failed_collections"],
        "verified_seats": seats["verified"],
        "spare_takeovers": faults["spare_takeovers"],
        "planned_restart_recoveries": faults["planned_restart_recoveries"],
        "controller_network_recoveries": faults["controller_network_recoveries"],
        "capacity_margin_accepted": evidence["thresholds"]["capacity_margin_accepted"],
    }


def validate_capacity_evidence(
    document: Any,
    *,
    expected_revision: str | None = None,
    expected_tree: str | None = None,
    expected_components: dict[str, Any] | None = None,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    evidence = exact_keys(
        document,
        {
            "$schema",
            "schema_version",
            "status",
            "session_id",
            "source",
            "components",
            "environment",
            "window",
            "seats",
            "workload",
            "faults",
            "isolation",
            "shutdown",
            "metrics",
            "thresholds",
            "probes",
            "artifacts",
        },
        "capacity evidence",
    )
    if evidence["$schema"] != "v1-capacity-evidence.schema.json":
        raise EvidenceError("unexpected capacity evidence schema")
    if evidence["schema_version"] != 1 or evidence["status"] != "passed":
        raise EvidenceError("capacity evidence must be schema version 1 and passed")
    require_pattern(evidence["session_id"], HEX64, "session_id")

    source = exact_keys(evidence["source"], {"revision", "tree"}, "source")
    revision = require_pattern(source["revision"], HEX40, "source.revision")
    tree = require_pattern(source["tree"], HEX40, "source.tree")
    if expected_revision is not None and revision != expected_revision:
        raise EvidenceError("capacity evidence revision differs from the qualification report")
    if expected_tree is not None and tree != expected_tree:
        raise EvidenceError("capacity evidence tree differs from the candidate source tree")

    components = exact_keys(
        evidence["components"],
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
        raise EvidenceError("desktop source revision differs from capacity source revision")
    require_pattern(components["hydro_plugin_sha256"], HEX64, "Hydro plugin SHA256")
    if expected_components is not None and components != expected_components:
        raise EvidenceError("capacity evidence components differ from the qualification report")

    probes = exact_keys(evidence["probes"], PROBE_KINDS, "probes")
    for kind, digest in probes.items():
        require_pattern(digest, HEX64, f"probes.{kind}")

    environment = exact_keys(
        evidence["environment"],
        {"profile", "instance_type", "region", "network_profile_sha256"},
        "environment",
    )
    if environment["profile"] != PROFILE:
        raise EvidenceError("capacity evidence profile differs")
    require_pattern(environment["instance_type"], SAFE_TEXT, "environment.instance_type")
    require_pattern(environment["region"], SAFE_TEXT, "environment.region")
    require_pattern(environment["network_profile_sha256"], HEX64, "network profile SHA256")

    window = exact_keys(
        evidence["window"],
        {"started_at", "ended_at", "duration_seconds", "sample_interval_seconds", "sample_count"},
        "window",
    )
    started = parse_utc(window["started_at"], "window.started_at")
    ended = parse_utc(window["ended_at"], "window.ended_at")
    duration = require_int(window["duration_seconds"], "window.duration_seconds", minimum=3600)
    elapsed = int((ended - started).total_seconds())
    if elapsed < duration or elapsed > duration + 5:
        raise EvidenceError("capacity window timestamps do not match duration_seconds")
    interval = require_int(window["sample_interval_seconds"], "window.sample_interval_seconds", minimum=1)
    if interval > 60:
        raise EvidenceError("sample interval must not exceed 60 seconds")
    samples = require_int(window["sample_count"], "window.sample_count", minimum=1)
    if samples < duration // interval + 1:
        raise EvidenceError("capacity sample series is too sparse for the declared window")

    seats = exact_keys(
        evidence["seats"],
        {"formal", "spare", "verified", "unique_container_ids", "unexpected_restart_events", "cross_seat_access_failures"},
        "seats",
    )
    expected_seats = {"formal": 15, "spare": 2, "verified": 17, "unique_container_ids": 17}
    for key, expected in expected_seats.items():
        if require_int(seats[key], f"seats.{key}") != expected:
            raise EvidenceError(f"seats.{key} must equal {expected}")
    for key in ("unexpected_restart_events", "cross_seat_access_failures"):
        if require_int(seats[key], f"seats.{key}") != 0:
            raise EvidenceError(f"seats.{key} must be zero")

    workload = exact_keys(
        evidence["workload"],
        {"login_successes", "material_open_successes", "compile_successes", "submission_successes", "failed_submissions", "collection_successes", "failed_collections", "final_source_mismatches"},
        "workload",
    )
    minimums = {
        "login_successes": 15,
        "material_open_successes": 15,
        "compile_successes": 45,
        "submission_successes": 45,
        "collection_successes": 15,
    }
    for key, minimum in minimums.items():
        require_int(workload[key], f"workload.{key}", minimum=minimum)
    for key in ("failed_submissions", "failed_collections", "final_source_mismatches"):
        if require_int(workload[key], f"workload.{key}") != 0:
            raise EvidenceError(f"workload.{key} must be zero")

    faults = exact_keys(
        evidence["faults"],
        {"spare_takeovers", "spare_takeovers_recovered", "planned_restart_events", "planned_restart_recoveries", "controller_network_interruptions", "controller_network_recoveries"},
        "faults",
    )
    pairs = (
        ("spare_takeovers", "spare_takeovers_recovered"),
        ("planned_restart_events", "planned_restart_recoveries"),
        ("controller_network_interruptions", "controller_network_recoveries"),
    )
    for attempted_key, recovered_key in pairs:
        attempted = require_int(faults[attempted_key], f"faults.{attempted_key}", minimum=1)
        recovered = require_int(faults[recovered_key], f"faults.{recovered_key}", minimum=1)
        if recovered != attempted:
            raise EvidenceError(f"faults.{recovered_key} must equal faults.{attempted_key}")

    isolation = exact_keys(
        evidence["isolation"],
        {"ordinary_oj_errors", "ordinary_oj_restarts", "ordinary_oj_pid_changes", "credential_leaks", "result_leaks"},
        "isolation",
    )
    for key, value in isolation.items():
        if require_int(value, f"isolation.{key}") != 0:
            raise EvidenceError(f"isolation.{key} must be zero")

    shutdown = exact_keys(
        evidence["shutdown"],
        {"active_seats", "managed_rules", "conflict_rules", "cloud_state", "delivery_queues", "notification_queues"},
        "shutdown",
    )
    for key in ("active_seats", "managed_rules", "conflict_rules", "delivery_queues", "notification_queues"):
        if require_int(shutdown[key], f"shutdown.{key}") != 0:
            raise EvidenceError(f"shutdown.{key} must be zero")
    if shutdown["cloud_state"] != "STOPPED":
        raise EvidenceError("shutdown.cloud_state must equal STOPPED")

    metric_names = {
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
    metrics = exact_keys(evidence["metrics"], metric_names, "metrics")
    numeric_metrics = {key: require_number(value, f"metrics.{key}") for key, value in metrics.items()}
    for key in ("host_cpu_peak_percent", "host_memory_peak_percent", "packet_loss_percent"):
        if numeric_metrics[key] > 100:
            raise EvidenceError(f"metrics.{key} must not exceed 100")
    if numeric_metrics["container_memory_peak_bytes"] <= 0:
        raise EvidenceError("container memory peak must be positive")
    if numeric_metrics["rtt_p95_ms"] <= 0 or numeric_metrics["key_to_frame_p50_ms"] <= 0:
        raise EvidenceError("latency metrics must be positive")
    if numeric_metrics["key_to_frame_p95_ms"] < numeric_metrics["key_to_frame_p50_ms"]:
        raise EvidenceError("key-to-frame p95 must be >= p50")

    threshold_keys = THRESHOLD_VALUE_NAMES | {
        "thresholds_sha256",
        "capacity_margin_accepted",
    }
    thresholds = exact_keys(evidence["thresholds"], threshold_keys, "thresholds")
    require_pattern(thresholds["thresholds_sha256"], HEX64, "thresholds SHA256")
    if thresholds["thresholds_sha256"] != threshold_policy_sha256(thresholds):
        raise EvidenceError("thresholds SHA256 is not derived from the threshold values")
    require_true(thresholds["capacity_margin_accepted"], "thresholds.capacity_margin_accepted")
    threshold_map = {
        "host_cpu_peak_percent": "host_cpu_peak_percent_max",
        "host_memory_peak_percent": "host_memory_peak_percent_max",
        "container_memory_peak_bytes": "container_memory_peak_bytes_max",
        "egress_peak_mbps": "egress_peak_mbps_max",
        "rtt_p95_ms": "rtt_p95_ms_max",
        "packet_loss_percent": "packet_loss_percent_max",
        "websocket_reconnects": "websocket_reconnects_max",
        "key_to_frame_p95_ms": "key_to_frame_p95_ms_max",
    }
    for metric, threshold in threshold_map.items():
        maximum = require_number(thresholds[threshold], f"thresholds.{threshold}")
        if numeric_metrics[metric] > maximum:
            raise EvidenceError(f"metrics.{metric} exceeds its accepted threshold")

    artifacts = evidence["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != len(ARTIFACT_NAMES):
        raise EvidenceError("artifacts must contain the six required private evidence files")
    observed_names: set[str] = set()
    artifact_paths: dict[str, Path] = {}
    resolved_root = None
    if artifact_root is not None:
        if not artifact_root.is_dir() or artifact_root.is_symlink():
            raise EvidenceError("artifact root must be a real directory")
        resolved_root = artifact_root.resolve()
    for index, item in enumerate(artifacts):
        row = exact_keys(item, {"name", "reference", "sha256", "bytes"}, f"artifacts[{index}]")
        name = row["name"]
        if name not in ARTIFACT_NAMES or name in observed_names:
            raise EvidenceError("artifact names must be unique and use the fixed allowlist")
        observed_names.add(name)
        reference = safe_reference(row["reference"], f"artifacts[{index}].reference")
        digest = require_pattern(row["sha256"], HEX64, f"artifacts[{index}].sha256")
        size = require_int(row["bytes"], f"artifacts[{index}].bytes", minimum=1)
        if resolved_root is not None:
            unresolved = resolved_root
            for part in PurePosixPath(reference).parts:
                unresolved = unresolved / part
                if unresolved.is_symlink():
                    raise EvidenceError(f"artifact path contains a symlink: {reference}")
            path = unresolved.resolve()
            try:
                path.relative_to(resolved_root)
            except ValueError as exc:
                raise EvidenceError("artifact reference escapes the artifact root") from exc
            if not path.is_file() or path.is_symlink():
                raise EvidenceError(f"artifact is missing or unsafe: {reference}")
            if path.stat().st_size != size or sha256_file(path) != digest:
                raise EvidenceError(f"artifact bytes or SHA256 differ: {reference}")
            artifact_paths[name] = path
    if observed_names != ARTIFACT_NAMES:
        raise EvidenceError("capacity artifact set differs")
    if resolved_root is not None:
        validate_private_artifacts(evidence, artifact_paths)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    args = parser.parse_args()
    try:
        raw = args.evidence.read_bytes()
        evidence = validate_capacity_evidence(
            json.loads(raw.decode("utf-8")), artifact_root=args.artifact_root
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, EvidenceError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "passed",
                "source_revision": evidence["source"]["revision"],
                "evidence_sha256": hashlib.sha256(raw).hexdigest(),
                "summary": capacity_summary(evidence),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
