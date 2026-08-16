#!/usr/bin/env python3
"""Derive all capacity fault counts from DB, Docker, and signed action evidence."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import Any


EMBEDDED_CONFIG = None
NAMESPACE = "noi-v1-capacity-network-fault"
HEX24 = re.compile(r"[a-f0-9]{24}")
HEX64 = re.compile(r"[a-f0-9]{64}")
MARKER = re.compile(r"NOI-V1-QUAL-[A-Z0-9]{16,64}")
SIGNER = re.compile(r"[A-Za-z0-9_.@+-]{1,80}")
SSH_PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?")
SEAT_FIELDS = {
    "slot_no", "seat_key", "role", "state", "uid", "uname", "container_ref",
    "image_digest", "material_digest", "failure_count", "last_error",
    "reserved_at_ms", "released_at_ms", "frozen_at_ms", "collected_at_ms",
}


class FaultProbeError(RuntimeError):
    pass


def exact(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise FaultProbeError(f"{label} field set differs")
    return value


def absolute(value: object, label: str) -> str:
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or \
            "\x00" in value or ".." in PurePosixPath(value).parts or "//" in value:
        raise FaultProbeError(f"{label} must be a normalized absolute path")
    return value


def validate_config(value: object) -> dict:
    row = exact(value, {
        "schema_version", "qualification_marker", "database_path", "contest_id",
        "expected_pool_revision", "failed_slot", "replacement_slot",
        "seat_inventory_probe", "seat_inventory_probe_sha256",
        "controller_probe_target_sha256", "network_action_envelope",
        "network_action_receipt",
        "network_action_agent_sha256",
        "network_action_signer", "network_action_public_key",
        "network_action_max_age_seconds", "ssh_keygen_path", "capacity_session_dir",
    }, "fault probe configuration")
    if row["schema_version"] != 1 or not isinstance(row["qualification_marker"], str) or \
            not MARKER.fullmatch(row["qualification_marker"]):
        raise FaultProbeError("fault qualification marker is invalid")
    for key in ("database_path", "seat_inventory_probe", "network_action_envelope",
                "network_action_receipt", "ssh_keygen_path", "capacity_session_dir"):
        row[key] = absolute(row[key], key)
    if len({row[key] for key in (
            "database_path", "seat_inventory_probe", "network_action_envelope",
            "network_action_receipt", "ssh_keygen_path", "capacity_session_dir")}) != 6:
        raise FaultProbeError("fault private paths must differ")
    if not isinstance(row["contest_id"], str) or not HEX24.fullmatch(row["contest_id"]):
        raise FaultProbeError("fault contest identity is invalid")
    for key in ("seat_inventory_probe_sha256", "controller_probe_target_sha256",
                "network_action_agent_sha256"):
        if not isinstance(row[key], str) or not HEX64.fullmatch(row[key]):
            raise FaultProbeError(f"{key} is invalid")
    revision = row["expected_pool_revision"]
    failed, replacement = row["failed_slot"], row["replacement_slot"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1 or \
            isinstance(failed, bool) or not isinstance(failed, int) or not 1 <= failed <= 15 or \
            isinstance(replacement, bool) or not isinstance(replacement, int) or \
            not 16 <= replacement <= 17:
        raise FaultProbeError("fault pool revision or slot binding is invalid")
    if not isinstance(row["network_action_signer"], str) or \
            not SIGNER.fullmatch(row["network_action_signer"]) or \
            not isinstance(row["network_action_public_key"], str) or \
            not SSH_PUBLIC_KEY.fullmatch(row["network_action_public_key"]):
        raise FaultProbeError("fault network signer is invalid")
    age = row["network_action_max_age_seconds"]
    if isinstance(age, bool) or not isinstance(age, int) or not 1 <= age <= 1800:
        raise FaultProbeError("fault network action maximum age is invalid")
    return row


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise FaultProbeError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FaultProbeError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise FaultProbeError(f"{label} is invalid")
    return parsed.astimezone(timezone.utc)


def safe_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or \
                stat.S_IMODE(info.st_mode) & 0o022:
            raise FaultProbeError("fault private path ancestor is unsafe")


def private_file(path: Path, label: str, *, executable: bool = False) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    info = resolved.stat(); safe_ancestors(resolved)
    if requested != resolved or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or \
            info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022 or \
            (executable and not os.access(resolved, os.X_OK)):
        raise FaultProbeError(f"{label} metadata is unsafe")
    return resolved


def read_bounded(path: Path, label: str, limit: int = 4 * 1024 * 1024) -> bytes:
    path = private_file(path, label)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) |
                         getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not 0 < info.st_size <= limit:
            raise FaultProbeError(f"{label} size is invalid")
        raw = os.read(descriptor, info.st_size + 1)
        if len(raw) != info.st_size:
            raise FaultProbeError(f"{label} changed while reading")
        return raw
    finally:
        os.close(descriptor)


def private_directory(path: Path, label: str) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    info = resolved.stat(); safe_ancestors(resolved / "leaf")
    if requested != resolved or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or \
            (platform.system().lower() == "linux" and
             (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077)):
        raise FaultProbeError(f"{label} metadata is unsafe")
    return resolved


def verify_sample_window(config: dict, started: datetime, completed: datetime) -> None:
    directory = private_directory(Path(config["capacity_session_dir"]), "capacity session directory")
    try:
        session = exact(json.loads(read_bounded(directory / "session.json", "capacity session").decode()), {
            "$schema", "schema_version", "session_id", "created_at", "source", "components",
            "environment", "thresholds", "probes", "duration_seconds", "sample_interval_seconds",
        }, "capacity session")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FaultProbeError("capacity session is not strict JSON") from exc
    duration, interval = session["duration_seconds"], session["sample_interval_seconds"]
    if session["$schema"] != "v1-capacity-session.schema.json" or session["schema_version"] != 1 or \
            not isinstance(session["session_id"], str) or not HEX64.fullmatch(session["session_id"]) or \
            isinstance(duration, bool) or not isinstance(duration, int) or duration < 3600 or \
            isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 60:
        raise FaultProbeError("capacity session identity differs")
    created = timestamp(session["created_at"], "capacity session created_at")
    samples_dir = private_directory(directory / "samples", "capacity sample directory")
    files = sorted(samples_dir.iterdir()); planned = duration // interval + 1
    if len(files) != planned or [path.name for path in files] != \
            [f"{index:06d}.json" for index in range(1, planned + 1)]:
        raise FaultProbeError("capacity sample window is incomplete")
    bounds = []
    for expected_sequence, path in ((1, files[0]), (planned, files[-1])):
        try:
            sample = exact(json.loads(read_bounded(path, "capacity boundary sample").decode()), {
                "schema_version", "kind", "session_id", "sequence", "observed_at", "metrics",
                "telemetry", "ordinary_oj", "collector",
            }, "capacity boundary sample")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FaultProbeError("capacity boundary sample is not strict JSON") from exc
        ordinary = sample["ordinary_oj"]
        if sample["schema_version"] != 1 or sample["kind"] != "capacity_sample" or \
                sample["session_id"] != session["session_id"] or sample["sequence"] != expected_sequence or \
                not isinstance(ordinary, dict) or ordinary.get("qualification_marker") != config["qualification_marker"]:
            raise FaultProbeError("capacity boundary sample identity differs")
        bounds.append(timestamp(sample["observed_at"], "capacity boundary sample observed_at"))
    if not created <= bounds[0] <= started < completed <= bounds[1]:
        raise FaultProbeError("network fault occurred outside the capacity sample window")


def sha256_file(path: Path, label: str, *, executable: bool = False) -> str:
    resolved = private_file(path, label, executable=executable)
    return hashlib.sha256(read_bounded(resolved, label, 8 * 1024 * 1024)).hexdigest()


def verify_signature(config: dict, envelope: dict, payload: dict) -> None:
    signature_value = envelope["signature_base64"]
    if not isinstance(signature_value, str) or not re.fullmatch(
            r"[A-Za-z0-9+/=]{40,131072}", signature_value):
        raise FaultProbeError("fault network signature is invalid")
    try:
        signature_raw = base64.b64decode(signature_value, validate=True)
    except ValueError as exc:
        raise FaultProbeError("fault network signature is invalid") from exc
    binary = private_file(Path(config["ssh_keygen_path"]), "ssh-keygen", executable=True)
    with tempfile.TemporaryDirectory(prefix="noi-v1-fault-verify-") as temporary:
        allowed = Path(temporary) / "allowed_signers"; signature = Path(temporary) / "payload.sig"
        allowed.write_text(
            f"{config['network_action_signer']} {config['network_action_public_key']}\n"
        )
        signature.write_bytes(signature_raw); os.chmod(allowed, 0o600); os.chmod(signature, 0o600)
        try:
            result = subprocess.run(
                [str(binary), "-Y", "verify", "-f", str(allowed), "-I",
                 config["network_action_signer"], "-n", NAMESPACE, "-s", str(signature)],
                input=canonical(payload), capture_output=True, check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FaultProbeError("fault network signature verification could not complete") from exc
    if result.returncode:
        raise FaultProbeError("fault network signature is invalid")


def verify_network_envelope(config: dict, now: datetime) -> dict:
    raw = read_bounded(Path(config["network_action_envelope"]), "fault network envelope")
    try:
        envelope = exact(json.loads(raw.decode()), {
            "schema_version", "namespace", "signer", "payload", "signature_base64"
        }, "fault network envelope")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FaultProbeError("fault network envelope is not strict JSON") from exc
    if raw != canonical(envelope) or envelope["schema_version"] != 1 or \
            envelope["namespace"] != NAMESPACE or \
            envelope["signer"] != config["network_action_signer"]:
        raise FaultProbeError("fault network envelope identity differs")
    payload = exact(envelope["payload"], {
        "schema_version", "qualification_marker", "contest_id_sha256",
        "seat_inventory_probe_sha256", "controller_probe_target_sha256",
        "fault_method", "operation_receipt_sha256", "observed_at", "events",
    }, "fault network payload")
    if payload["schema_version"] != 1 or \
            payload["qualification_marker"] != config["qualification_marker"] or \
            payload["contest_id_sha256"] != hashlib.sha256(config["contest_id"].encode()).hexdigest() or \
            payload["seat_inventory_probe_sha256"] != config["seat_inventory_probe_sha256"] or \
            payload["controller_probe_target_sha256"] != config["controller_probe_target_sha256"] or \
            payload["fault_method"] != "controller-egress-deny" or \
            not isinstance(payload["operation_receipt_sha256"], str) or \
            not HEX64.fullmatch(payload["operation_receipt_sha256"]):
        raise FaultProbeError("fault network payload identity differs")
    observed = timestamp(payload["observed_at"], "fault network observed_at")
    age = (now - observed).total_seconds()
    if age < -5 or age > config["network_action_max_age_seconds"]:
        raise FaultProbeError("fault network payload is stale or future-dated")
    events = payload["events"]
    expected = [
        ("before_interrupt", 3, 0),
        ("during_interrupt", 0, 3),
        ("after_recovery", 3, 0),
    ]
    if not isinstance(events, list) or len(events) != 3:
        raise FaultProbeError("fault network event sequence differs")
    times = []
    for item, (phase, successes, failures) in zip(events, expected):
        item = exact(item, {
            "phase", "observed_at", "consecutive_probe_successes",
            "consecutive_probe_failures",
        }, "fault network event")
        if item["phase"] != phase or item["consecutive_probe_successes"] != successes or \
                item["consecutive_probe_failures"] != failures:
            raise FaultProbeError("fault network event result differs")
        times.append(timestamp(item["observed_at"], "fault network event observed_at"))
    if not times[0] < times[1] < times[2] <= observed or \
            (times[2] - times[0]).total_seconds() > 600:
        raise FaultProbeError("fault network event timing differs")
    receipt_raw = read_bounded(
        Path(config["network_action_receipt"]), "fault network operation receipt"
    )
    try:
        receipt = exact(json.loads(receipt_raw.decode()), {
            "schema_version", "qualification_marker", "contest_id_sha256",
            "seat_inventory_probe_sha256", "controller_probe_target_sha256",
            "agent_sha256", "controller_identity_sha256", "rule_identity_sha256", "started_at",
            "rule_installed_at", "rule_removed_at", "completed_at",
            "before_successes", "during_failures", "after_successes",
        }, "fault network operation receipt")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FaultProbeError("fault network operation receipt is not strict JSON") from exc
    if receipt_raw != canonical(receipt) or hashlib.sha256(receipt_raw).hexdigest() != \
            payload["operation_receipt_sha256"] or receipt["schema_version"] != 1 or \
            receipt["qualification_marker"] != config["qualification_marker"] or \
            receipt["contest_id_sha256"] != payload["contest_id_sha256"] or \
            receipt["seat_inventory_probe_sha256"] != config["seat_inventory_probe_sha256"] or \
            receipt["controller_probe_target_sha256"] != config["controller_probe_target_sha256"] or \
            receipt["agent_sha256"] != config["network_action_agent_sha256"] or \
            any(not isinstance(receipt[key], str) or not HEX64.fullmatch(receipt[key])
                for key in ("controller_identity_sha256", "rule_identity_sha256")) or \
            (receipt["before_successes"], receipt["during_failures"], receipt["after_successes"]) != (3, 3, 3):
        raise FaultProbeError("fault network operation receipt identity differs")
    receipt_times = [timestamp(receipt[key], f"fault receipt {key}") for key in (
        "started_at", "rule_installed_at", "rule_removed_at", "completed_at"
    )]
    if not receipt_times[0] <= times[0] < receipt_times[1] <= times[1] < \
            receipt_times[2] <= times[2] <= receipt_times[3] or \
            receipt["completed_at"] != payload["observed_at"]:
        raise FaultProbeError("fault network operation receipt timing differs")
    verify_sample_window(config, receipt_times[0], receipt_times[3])
    verify_signature(config, envelope, payload)
    return payload


def run_seat_probe(config: dict) -> dict:
    path = Path(config["seat_inventory_probe"])
    if sha256_file(path, "seat inventory probe", executable=True) != \
            config["seat_inventory_probe_sha256"]:
        raise FaultProbeError("seat inventory probe SHA256 differs")
    environment = {
        "HOME": "/root", "USER": "root", "LOGNAME": "root", "SHELL": "/bin/sh",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
    }
    try:
        result = subprocess.run(
            [str(path)], capture_output=True, check=False, timeout=90, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FaultProbeError("seat inventory probe could not complete") from exc
    if result.returncode or result.stderr or len(result.stdout) > 256 * 1024:
        raise FaultProbeError("seat inventory probe failed")
    try:
        value = exact(json.loads(result.stdout), {
            "observed_at", "formal_container_ids", "spare_container_ids",
            "verified_container_ids", "unexpected_restart_events",
            "planned_restart_events", "planned_restart_recoveries",
            "cross_seat_access_failures",
        }, "seat inventory result")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FaultProbeError("seat inventory result is invalid") from exc
    timestamp(value["observed_at"], "seat inventory observed_at")
    if len(value["formal_container_ids"]) != 15 or len(value["spare_container_ids"]) != 2 or \
            len(value["verified_container_ids"]) != 17 or \
            value["unexpected_restart_events"] != 0 or \
            value["planned_restart_events"] != 1 or \
            value["planned_restart_recoveries"] != 1 or \
            value["cross_seat_access_failures"] != 0:
        raise FaultProbeError("seat inventory does not prove the required recovery")
    return value


def seat(item: object, label: str) -> dict:
    return exact(item, SEAT_FIELDS, label)


def receipt_seat(item: object, label: str) -> dict:
    value = exact(item, SEAT_FIELDS | {"revision"}, label)
    return {key: value[key] for key in SEAT_FIELDS}


def verify_pool_history(config: dict) -> None:
    path = private_file(Path(config["database_path"]), "fault database")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise FaultProbeError("fault database integrity check failed")
        contest = connection.execute(
            "SELECT state FROM contests WHERE tid=?", (config["contest_id"],)
        ).fetchall()
        pool_rows = connection.execute(
            "SELECT revision,state_json FROM seat_pools WHERE tid=?", (config["contest_id"],)
        ).fetchall()
        if len(contest) != 1 or contest[0]["state"] != "ready" or len(pool_rows) != 1:
            raise FaultProbeError("fault contest or pool state differs")
        if int(pool_rows[0]["revision"]) != config["expected_pool_revision"] + 3:
            raise FaultProbeError("fault pool revision does not prove replace plus repair")
        try:
            state = exact(json.loads(pool_rows[0]["state_json"]), {
                "schema_version", "tid", "max_participants", "spare_count", "begin_at_ms",
                "release_at_ms", "revision", "seats", "receipts",
            }, "fault pool state")
        except json.JSONDecodeError as exc:
            raise FaultProbeError("fault pool state is invalid JSON") from exc
        if state["schema_version"] != 1 or state["tid"] != config["contest_id"] or \
                state["max_participants"] != 15 or state["spare_count"] != 2 or \
                state["revision"] != config["expected_pool_revision"] + 3 or \
                not isinstance(state["seats"], list) or len(state["seats"]) != 17 or \
                not isinstance(state["receipts"], list):
            raise FaultProbeError("fault pool identity or capacity differs")
        receipts = {}
        for item in state["receipts"]:
            item = exact(item, {"command_id", "fingerprint", "revision", "value"}, "fault receipt")
            if not isinstance(item["command_id"], str) or item["command_id"] in receipts or \
                    not isinstance(item["fingerprint"], str) or not HEX64.fullmatch(item["fingerprint"]):
                raise FaultProbeError("fault receipt identity differs")
            receipts[item["command_id"]] = item
        base = config["expected_pool_revision"]
        failed_slot, replacement_slot = config["failed_slot"], config["replacement_slot"]
        replace_id = f"replace:{config['contest_id']}:r{base}:{failed_slot}"
        replace = receipts.get(replace_id)
        if not replace or replace["revision"] != base + 1:
            raise FaultProbeError("fault replacement receipt is missing")
        value = exact(replace["value"], {"failed", "replacement", "revision"}, "replacement receipt value")
        failed = seat(value["failed"], "replacement failed seat")
        replacement = seat(value["replacement"], "replacement spare seat")
        uid = replacement.get("uid")
        if value["revision"] != base + 1 or failed["slot_no"] != failed_slot or \
                failed["state"] != "planned" or failed["uid"] is not None or \
                failed["failure_count"] < 1 or replacement["slot_no"] != replacement_slot or \
                replacement["role"] != "primary" or failed["role"] != "spare" or \
                replacement["state"] not in {"reserved", "released"} or \
                isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
            raise FaultProbeError("fault replacement receipt semantics differ")
        failure_count = failed["failure_count"]
        warm_id = f"repair:warm:{config['contest_id']}:{failed_slot}:failure:{failure_count}"
        verify_id = f"repair:verify:{config['contest_id']}:{failed_slot}:failure:{failure_count}"
        warm, verified = receipts.get(warm_id), receipts.get(verify_id)
        if not warm or warm["revision"] != base + 2 or not verified or verified["revision"] != base + 3:
            raise FaultProbeError("fault capacity repair receipts are missing")
        warm_value = receipt_seat(warm["value"], "fault warm receipt value")
        verified_value = receipt_seat(verified["value"], "fault verify receipt value")
        if warm_value["state"] != "warming" or verified_value["state"] != "verified" or \
                warm_value["slot_no"] != failed_slot or verified_value["slot_no"] != failed_slot or \
                warm_value["uid"] is not None or verified_value["uid"] is not None or \
                verified_value["failure_count"] != failure_count or \
                verified_value["role"] != "spare" or \
                not verified_value["container_ref"]:
            raise FaultProbeError("fault capacity repair semantics differ")
        by_slot = {item["slot_no"]: seat(item, "current fault seat") for item in state["seats"]}
        if set(by_slot) != set(range(1, 18)) or by_slot[failed_slot] != verified_value or \
                by_slot[replacement_slot]["uid"] != uid or \
                by_slot[replacement_slot]["container_ref"] != replacement["container_ref"] or \
                sum(item["uid"] is not None for item in by_slot.values()) != 15 or \
                sum(item["role"] == "primary" for item in by_slot.values()) != 15 or \
                sum(item["role"] == "spare" for item in by_slot.values()) != 2 or \
                sum(item["state"] == "verified" and item["uid"] is None for item in by_slot.values()) != 2:
            raise FaultProbeError("fault terminal pool does not restore 15 plus 2")
        resources = connection.execute(
            "SELECT slot_no,container,image_digest,material_digest FROM seat_pool_resources "
            "WHERE tid=? ORDER BY slot_no", (config["contest_id"],)
        ).fetchall()
        assignments = connection.execute(
            "SELECT uid,container FROM seats WHERE tid=?", (config["contest_id"],)
        ).fetchall()
        if len(resources) != 17 or {int(item["slot_no"]) for item in resources} != set(range(1, 18)) or \
                len(assignments) != 15:
            raise FaultProbeError("fault terminal resource or assignment count differs")
        resource_by_slot = {int(item["slot_no"]): item for item in resources}
        if resource_by_slot[failed_slot]["container"] != verified_value["container_ref"] or \
                resource_by_slot[replacement_slot]["container"] != replacement["container_ref"] or \
                len([item for item in assignments if int(item["uid"]) == uid and
                     item["container"] == replacement["container_ref"]]) != 1:
            raise FaultProbeError("fault terminal resource binding differs")
    finally:
        connection.close()


def collect(config: dict, *, now: datetime | None = None) -> dict:
    row = validate_config(config)
    current = now or datetime.now(timezone.utc)
    verify_pool_history(row)
    run_seat_probe(row)
    verify_network_envelope(row, current)
    return {
        "observed_at": current.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "spare_takeovers": 1,
        "spare_takeovers_recovered": 1,
        "planned_restart_events": 1,
        "planned_restart_recoveries": 1,
        "controller_network_interruptions": 1,
        "controller_network_recoveries": 1,
    }


def main() -> int:
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise FaultProbeError("fault probe requires Linux root")
        if EMBEDDED_CONFIG is None:
            raise FaultProbeError("fault probe is not frozen")
        print(json.dumps(collect(EMBEDDED_CONFIG), sort_keys=True, separators=(",", ":")))
        return 0
    except (FaultProbeError, OSError, sqlite3.Error) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
