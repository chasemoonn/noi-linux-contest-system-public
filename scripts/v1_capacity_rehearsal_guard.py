#!/usr/bin/env python3
"""Read-only, fail-closed phase guard for one NOI V1 capacity rehearsal."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import sys


PROBES = ("measurement", "seat_inventory", "workload_events", "fault_events",
          "ordinary_oj_observations", "shutdown_observation")
RUNTIME_FACTS = {"seat_inventory", "fault_events"}
TERMINAL_FACTS = {"workload_events", "ordinary_oj_observations", "shutdown_observation"}
HEX64 = re.compile(r"[a-f0-9]{64}")


class GuardError(RuntimeError):
    pass


def exact(value, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys: raise GuardError(f"{label} field set differs")
    return value


def absolute(value, label: str) -> str:
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or "\0" in value or \
            ".." in PurePosixPath(value).parts or "//" in value:
        raise GuardError(f"{label} must be a normalized absolute path")
    return value


def validate_config(value) -> dict:
    row = exact(value, {"schema_version", "identity_path", "session_dir", "probe_paths",
                        "action_agents", "action_outputs"}, "capacity guard configuration")
    if row["schema_version"] != 1: raise GuardError("capacity guard schema differs")
    for key in ("identity_path", "session_dir"): row[key] = absolute(row[key], key)
    probes = exact(row["probe_paths"], set(PROBES), "capacity guard probes")
    for key in PROBES: probes[key] = absolute(probes[key], f"probe {key}")
    agents = exact(row["action_agents"], {"workload", "network"}, "capacity guard action agents")
    for key in ("workload", "network"):
        agent = exact(agents[key], {"path", "sha256"}, f"capacity guard {key} agent")
        agent["path"] = absolute(agent["path"], f"{key} agent path")
        if not isinstance(agent["sha256"], str) or not HEX64.fullmatch(agent["sha256"]):
            raise GuardError(f"{key} agent SHA256 is invalid")
    outputs = exact(row["action_outputs"], {"workload_receipt", "workload_envelope",
                    "network_receipt", "network_envelope"}, "capacity guard action outputs")
    for key in outputs: outputs[key] = absolute(outputs[key], key)
    paths = [row["identity_path"], row["session_dir"], *probes.values(),
             *(item["path"] for item in agents.values()), *outputs.values()]
    if len(paths) != len(set(paths)): raise GuardError("capacity guard private paths must differ")
    return row


def safe_ancestors(path: Path, *, include_leaf=False) -> None:
    current = Path(path.anchor); parts = path.parts[1:] if include_leaf else path.parts[1:-1]
    for part in parts:
        current /= part; info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or \
                (platform.system().lower() == "linux" and
                 (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022)):
            raise GuardError("capacity guard path ancestor is unsafe")


def read_file(path: Path, label: str, limit=16 * 1024 * 1024) -> bytes:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True); safe_ancestors(resolved)
    info = resolved.stat()
    if requested != resolved or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or \
            (platform.system().lower() == "linux" and
             (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022)) or not 0 < info.st_size <= limit:
        raise GuardError(f"{label} metadata is unsafe")
    descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor); blocks = []; remaining = before.st_size + 1
        while remaining > 0:
            block = os.read(descriptor, min(65536, remaining))
            if not block: break
            blocks.append(block); remaining -= len(block)
        raw = b"".join(blocks); after = os.fstat(descriptor)
        if (platform.system().lower() == "linux" and len(raw) != before.st_size) or not raw or \
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != \
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise GuardError(f"{label} changed while reading")
        return raw
    finally: os.close(descriptor)


def read_json(path: Path, label: str) -> dict:
    try: value = json.loads(read_file(path, label).decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise GuardError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict): raise GuardError(f"{label} root differs")
    return value


def digest(path: Path, label: str) -> str: return hashlib.sha256(read_file(path, label)).hexdigest()


def require_private_directory(path: Path, label: str) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True); info = resolved.stat()
    safe_ancestors(resolved, include_leaf=True)
    if requested != resolved or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or \
            (platform.system().lower() == "linux" and
             (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077)):
        raise GuardError(f"{label} metadata is unsafe")
    return resolved


def action_state(config: dict) -> dict[str, bool]:
    result = {}
    for kind in ("workload", "network"):
        agent = config["action_agents"][kind]
        if digest(Path(agent["path"]), f"{kind} action agent") != agent["sha256"]:
            raise GuardError(f"{kind} action agent SHA256 differs")
        receipt = Path(config["action_outputs"][f"{kind}_receipt"])
        envelope = Path(config["action_outputs"][f"{kind}_envelope"])
        if os.path.lexists(receipt) != os.path.lexists(envelope):
            raise GuardError(f"{kind} action output set is partial")
        result[kind] = os.path.lexists(receipt)
        if result[kind]:
            receipt_value = read_json(receipt, f"{kind} action receipt")
            if receipt_value.get("agent_sha256") != agent["sha256"]:
                raise GuardError(f"{kind} action receipt agent SHA256 differs")
            read_json(envelope, f"{kind} action envelope")
    return result


def inspect(config: dict) -> dict:
    identity = read_json(Path(config["identity_path"]), "capacity identity")
    probe_digests = identity.get("probes")
    if not isinstance(probe_digests, dict) or set(probe_digests) != set(PROBES):
        raise GuardError("capacity identity probe set differs")
    for kind in PROBES:
        if probe_digests[kind] != digest(Path(config["probe_paths"][kind]), f"{kind} probe"):
            raise GuardError(f"{kind} probe SHA256 differs")
    actions = action_state(config); session_path = Path(config["session_dir"])
    if not os.path.lexists(session_path):
        if any(actions.values()): raise GuardError("action evidence exists before session initialization")
        return {"status": "ready", "phase": "initialize", "next_action": "initialize capacity session"}
    session = require_private_directory(session_path, "capacity session")
    session_json = read_json(session / "session.json", "capacity session identity")
    if any(session_json.get(key) != identity.get(key) for key in ("source", "components", "environment", "thresholds", "probes")):
        raise GuardError("capacity session differs from frozen identity")
    samples_dir = require_private_directory(session / "samples", "capacity sample directory")
    samples = sorted(samples_dir.iterdir())
    if any(not path.is_file() or path.is_symlink() or not re.fullmatch(r"[0-9]{6}[.]json", path.name) for path in samples):
        raise GuardError("capacity sample directory contains an unexpected entry")
    duration = session_json.get("duration_seconds"); interval = session_json.get("sample_interval_seconds")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 3600 or \
            isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 60:
        raise GuardError("capacity session timing differs")
    planned = duration // interval + 1
    if len(samples) > planned: raise GuardError("capacity sample count exceeds plan")
    raw_dir = require_private_directory(session / "raw", "capacity fact directory")
    facts = {path.stem for path in raw_dir.iterdir() if path.name != "sample_series.json"}
    if not facts <= RUNTIME_FACTS | TERMINAL_FACTS: raise GuardError("capacity fact set contains an unexpected entry")
    finalized = os.path.lexists(session / "capacity-evidence.json")
    if len(samples) < planned:
        if facts or finalized: raise GuardError("closeout evidence exists before sampling completed")
        missing = [key for key, done in actions.items() if not done]
        return {"status": "running", "phase": "sampling_and_actions",
                "sample_count": len(samples), "planned_samples": planned,
                "missing_actions": missing,
                "next_action": "continue fixed-cadence sampling and finish both actions inside the sample window"}
    if not all(actions.values()):
        raise GuardError("both runtime actions must complete inside the sample window")
    if not RUNTIME_FACTS <= facts:
        if facts & TERMINAL_FACTS or finalized: raise GuardError("terminal facts exist before runtime closeout")
        return {"status": "waiting", "phase": "runtime_facts",
                "missing_facts": sorted(RUNTIME_FACTS - facts),
                "next_action": "record runtime seat and fault facts before collection"}
    if not TERMINAL_FACTS <= facts:
        if finalized: raise GuardError("capacity evidence finalized before terminal facts")
        return {"status": "waiting", "phase": "terminal_facts",
                "missing_facts": sorted(TERMINAL_FACTS - facts),
                "next_action": "finish collection, protection window, shutdown, and terminal facts"}
    if not finalized:
        return {"status": "waiting", "phase": "finalize", "next_action": "finalize and independently verify capacity evidence"}
    evidence = read_json(session / "capacity-evidence.json", "capacity evidence")
    if evidence.get("status") != "passed" or evidence.get("session_id") != session_json.get("session_id"):
        raise GuardError("capacity final evidence identity differs")
    return {"status": "ready", "phase": "independent_verification",
            "next_action": "run the independent capacity verifier and obtain reviewer sign-off"}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--config", required=True, type=Path); args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise GuardError("capacity rehearsal guard requires Linux root")
        config = validate_config(read_json(args.config, "capacity guard configuration"))
        print(json.dumps(inspect(config), sort_keys=True, separators=(",", ":"))); return 0
    except (GuardError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
