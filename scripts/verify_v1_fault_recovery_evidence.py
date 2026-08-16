#!/usr/bin/env python3
"""Combine signed fault actions with one verified 15+2 capacity session."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from verify_v1_capacity_evidence import EvidenceError as CapacityError, validate_capacity_evidence

NAMESPACE = "noi-v1-fault-recovery-actions"
SCENARIOS = {"control_restart", "collection_retry", "power_loss_recovery"}
HEX40 = re.compile(r"[a-f0-9]{40}")
HEX64 = re.compile(r"[a-f0-9]{64}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
MARKER = re.compile(r"NOI-V1-QUAL-[A-Z0-9]{16,64}")
SIGNER = re.compile(r"[A-Za-z0-9_.@+-]{1,80}")
PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?")

class EvidenceError(ValueError): pass

def exact(value, keys: set[str], label: str):
    if not isinstance(value, dict) or set(value) != keys:
        raise EvidenceError(f"{label} field set differs")
    return value

def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()

def parse_time(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(f"{label} is invalid")
    try: return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc: raise EvidenceError(f"{label} is invalid") from exc

def require_int(value, expected, label):
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise EvidenceError(f"{label} must equal {expected}")

def read_json(path: Path, label: str):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try: descriptor = os.open(path, flags)
    except OSError as exc: raise EvidenceError(f"{label} is unsafe") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 32 * 1024 * 1024:
            raise EvidenceError(f"{label} size or metadata is invalid")
        raw = os.read(descriptor, info.st_size + 1)
        if len(raw) != info.st_size: raise EvidenceError(f"{label} changed while reading")
    finally: os.close(descriptor)
    try: value = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise EvidenceError(f"{label} is invalid JSON") from exc
    return raw, value

def validate_identity(row, label):
    source = exact(row["source"], {"revision", "tree"}, f"{label}.source")
    if not HEX40.fullmatch(str(source["revision"])) or not HEX40.fullmatch(str(source["tree"])):
        raise EvidenceError(f"{label} source is invalid")
    components = exact(row["components"], {"orchestrator_image_digest", "desktop_image_id",
        "desktop_source_revision", "hydro_plugin_sha256"}, f"{label}.components")
    if not IMAGE.fullmatch(str(components["orchestrator_image_digest"])) or \
       not IMAGE.fullmatch(str(components["desktop_image_id"])) or \
       components["desktop_source_revision"] != source["revision"] or \
       not HEX64.fullmatch(str(components["hydro_plugin_sha256"])):
        raise EvidenceError(f"{label} components are invalid")
    return source, components

def verify_signature(row, ssh_keygen: Path):
    try:
        requested = Path(os.path.abspath(ssh_keygen)); resolved = requested.resolve(strict=True); info = resolved.stat()
    except OSError as exc: raise EvidenceError("ssh-keygen is unsafe") from exc
    if requested != resolved or not stat.S_ISREG(info.st_mode) or \
       (os.name == "posix" and (
           info.st_nlink != 1 or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022
       )):
        raise EvidenceError("ssh-keygen is unsafe")
    try: signature = base64.b64decode(row["signature"], validate=True)
    except (ValueError, TypeError) as exc: raise EvidenceError("signature encoding is invalid") from exc
    signed = dict(row); signed.pop("signature")
    with tempfile.TemporaryDirectory(prefix="v1-fault-verify-") as directory:
        allowed = Path(directory) / "allowed"; sig = Path(directory) / "signature"
        allowed.write_text(f"{row['signer']} {row['signing_public_key']}\n", encoding="utf-8")
        sig.write_bytes(signature)
        result = subprocess.run([str(resolved), "-Y", "verify", "-f", str(allowed),
            "-I", row["signer"], "-n", NAMESPACE, "-s", str(sig)], input=canonical(signed),
            capture_output=True, timeout=10, check=False)
    if result.returncode: raise EvidenceError("fault action signature is invalid")

def validate_action(value, scenario, capacity, ssh_keygen, expected_public_key, expected_agent_sha256):
    row = exact(value, {"$schema", "schema_version", "kind", "scenario", "session_id", "source",
        "components", "qualification_marker", "started_at", "ended_at", "collector", "signer",
        "signing_public_key", "payload", "signature"}, scenario)
    if row["$schema"] != "v1-fault-recovery-action-fact.schema.json" or row["schema_version"] != 1 \
       or row["kind"] != "fault_recovery_action" or row["scenario"] != scenario \
       or row["session_id"] != capacity["session_id"] or not MARKER.fullmatch(str(row["qualification_marker"])):
        raise EvidenceError(f"{scenario} identity differs")
    source, components = validate_identity(row, scenario)
    if source != capacity["source"] or components != capacity["components"]:
        raise EvidenceError(f"{scenario} source/components differ")
    collector = exact(row["collector"], {"mode", "agent_sha256"}, f"{scenario}.collector")
    if collector["mode"] != "trusted_action_agent" or not HEX64.fullmatch(str(collector["agent_sha256"])):
        raise EvidenceError(f"{scenario} collector differs")
    if collector["agent_sha256"] != expected_agent_sha256:
        raise EvidenceError(f"{scenario} action agent SHA256 differs")
    if not SIGNER.fullmatch(str(row["signer"])) or not PUBLIC_KEY.fullmatch(str(row["signing_public_key"])):
        raise EvidenceError(f"{scenario} signer differs")
    if row["signing_public_key"] != expected_public_key:
        raise EvidenceError(f"{scenario} signing public key differs from the external trust root")
    started, ended = parse_time(row["started_at"], "started_at"), parse_time(row["ended_at"], "ended_at")
    window_start = parse_time(capacity["window"]["started_at"], "window.started_at")
    window_end = parse_time(capacity["window"]["ended_at"], "window.ended_at")
    if not window_start <= started <= ended <= window_end + timedelta(minutes=30):
        raise EvidenceError(f"{scenario} occurred outside the qualification window")
    verify_signature(row, ssh_keygen)
    payload = row["payload"]
    common = {"ordinary_oj_errors", "ordinary_oj_restarts", "ordinary_oj_pid_changes",
              "duplicate_oj_records", "final_source_mismatches", "other_seat_failures"}
    scenario_keys = {
      "control_restart": {"restart_events", "restart_recoveries", "pending_jobs_before",
                          "pending_jobs_resumed", "controller_identity_preserved"},
      "collection_retry": {"injected_failures", "retry_attempts", "successful_deliveries",
                           "collection_receipt_unique"},
      "power_loss_recovery": {"durable_marker_created", "abrupt_termination_observed",
                              "startup_blocked_pending", "recovery_completed", "baseline_restored",
                              "active_seats", "managed_rules", "cloud_state"},
    }
    payload = exact(payload, common | scenario_keys[scenario], f"{scenario}.payload")
    for key in common: require_int(payload[key], 0, f"{scenario}.{key}")
    if scenario == "control_restart":
        require_int(payload["restart_events"], 1, "restart_events"); require_int(payload["restart_recoveries"], 1, "restart_recoveries")
        if not isinstance(payload["pending_jobs_before"], int) or payload["pending_jobs_before"] < 1 \
           or payload["pending_jobs_resumed"] != payload["pending_jobs_before"] or payload["controller_identity_preserved"] is not True:
            raise EvidenceError("control restart recovery differs")
    elif scenario == "collection_retry":
        require_int(payload["injected_failures"], 1, "injected_failures"); require_int(payload["retry_attempts"], 1, "retry_attempts")
        require_int(payload["successful_deliveries"], 1, "successful_deliveries")
        if payload["collection_receipt_unique"] is not True: raise EvidenceError("collection retry receipt differs")
    else:
        for key in ("durable_marker_created", "abrupt_termination_observed", "startup_blocked_pending", "recovery_completed", "baseline_restored"):
            if payload[key] is not True: raise EvidenceError(f"power loss {key} differs")
        require_int(payload["active_seats"], 0, "active_seats"); require_int(payload["managed_rules"], 0, "managed_rules")
        if payload["cloud_state"] != "STOPPED": raise EvidenceError("power loss cloud state differs")
    return row

def validate_combined(value, *, expected_revision=None, expected_components=None,
                      capacity=None, capacity_raw=None,
                      ssh_keygen=None):
    row = exact(value, {"$schema", "schema_version", "status", "session_id", "source", "components",
        "scenarios", "ordinary_oj_isolation", "signer", "signing_public_key",
        "signing_public_key_sha256", "action_agent_sha256", "actions", "inputs"}, "fault evidence")
    if row["$schema"] != "v1-fault-recovery-evidence.schema.json" or row["schema_version"] != 2 or row["status"] != "passed":
        raise EvidenceError("fault evidence identity differs")
    source, components = validate_identity(row, "fault evidence")
    if expected_revision and source["revision"] != expected_revision: raise EvidenceError("fault revision differs")
    if expected_components and components != expected_components: raise EvidenceError("fault components differ")
    scenarios = exact(row["scenarios"], {"control_restart", "desktop_reconnect", "single_seat_replace",
        "network_interruption", "collection_retry", "power_loss_recovery"}, "scenarios")
    if any(value is not True for value in scenarios.values()): raise EvidenceError("fault scenarios are not all passed")
    isolation = exact(row["ordinary_oj_isolation"], {"errors", "restarts", "pid_changes"}, "isolation")
    if any(isolation[key] != 0 for key in isolation): raise EvidenceError("ordinary OJ isolation differs")
    if not SIGNER.fullmatch(str(row["signer"])) or not PUBLIC_KEY.fullmatch(str(row["signing_public_key"])) or \
       not HEX64.fullmatch(str(row["signing_public_key_sha256"])) or \
       hashlib.sha256(row["signing_public_key"].encode()).hexdigest() != row["signing_public_key_sha256"]:
        raise EvidenceError("fault signer identity differs")
    agent_hashes = exact(row["action_agent_sha256"], SCENARIOS, "action agent SHA256")
    if any(not HEX64.fullmatch(str(value)) for value in agent_hashes.values()):
        raise EvidenceError("fault action agent SHA256 is invalid")
    actions = exact(row["actions"], SCENARIOS, "embedded fault actions")
    inputs = row["inputs"]
    if not isinstance(inputs, list) or len(inputs) != 4 or \
       {item.get("name") for item in inputs if isinstance(item, dict)} != {"capacity", *SCENARIOS}:
        raise EvidenceError("fault evidence inputs differ")
    for item in inputs:
        exact(item, {"name", "reference", "sha256"}, "fault evidence input")
        if not isinstance(item["reference"], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json", item["reference"]
        ) or not HEX64.fullmatch(str(item["sha256"])):
            raise EvidenceError("fault evidence input is invalid")
    input_rows = {item["name"]: item for item in inputs}
    for scenario in SCENARIOS:
        if hashlib.sha256(canonical(actions[scenario])).hexdigest() != input_rows[scenario]["sha256"]:
            raise EvidenceError("embedded fault action SHA256 differs")
    if capacity is not None:
        if ssh_keygen is None:
            ssh_keygen = Path(shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen")
        if capacity_raw is None or not isinstance(capacity_raw, bytes) or \
           hashlib.sha256(capacity_raw).hexdigest() != input_rows["capacity"]["sha256"]:
            raise EvidenceError("fault capacity evidence SHA256 differs")
        if capacity["session_id"] != row["session_id"] or capacity["source"] != source or \
           capacity["components"] != components:
            raise EvidenceError("fault capacity identity differs")
        for scenario in sorted(SCENARIOS):
            action = validate_action(
                actions[scenario], scenario, capacity, ssh_keygen,
                row["signing_public_key"], agent_hashes[scenario],
            )
            if action["signer"] != row["signer"]:
                raise EvidenceError("embedded fault action signer differs")
    return row

def read_public_key(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try: descriptor = os.open(path, flags)
    except OSError as exc: raise EvidenceError("signing public key is unsafe") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or \
           (os.name == "posix" and (info.st_uid not in {0, os.geteuid()} or stat.S_IMODE(info.st_mode) & 0o022)) or \
           not 1 < info.st_size <= 512:
            raise EvidenceError("signing public key is unsafe")
        raw = os.read(descriptor, info.st_size + 1)
        if len(raw) != info.st_size: raise EvidenceError("signing public key changed while reading")
    finally: os.close(descriptor)
    if not 1 < len(raw) <= 512: raise EvidenceError("signing public key size is invalid")
    try: value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc: raise EvidenceError("signing public key is not UTF-8") from exc
    if not PUBLIC_KEY.fullmatch(value): raise EvidenceError("signing public key is invalid")
    return value

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capacity", required=True, type=Path); parser.add_argument("--artifact-root", required=True, type=Path)
    for name in sorted(SCENARIOS): parser.add_argument("--" + name.replace("_", "-"), required=True, type=Path)
    for name in sorted(SCENARIOS): parser.add_argument("--" + name.replace("_", "-") + "-agent-sha256", required=True)
    parser.add_argument("--signing-public-key", required=True, type=Path)
    parser.add_argument("--ssh-keygen", default=shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen", type=Path); parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        capacity_raw, capacity_value = read_json(args.capacity, "capacity")
        capacity = validate_capacity_evidence(capacity_value, artifact_root=args.artifact_root)
        public_key = read_public_key(args.signing_public_key)
        facts = {}; raws = {}; signer = key = marker = None
        for scenario in sorted(SCENARIOS):
            agent_sha = getattr(args, scenario + "_agent_sha256")
            if not isinstance(agent_sha, str) or not HEX64.fullmatch(agent_sha):
                raise EvidenceError(f"{scenario} expected agent SHA256 is invalid")
            raw, value = read_json(getattr(args, scenario), scenario)
            fact = validate_action(value, scenario, capacity, args.ssh_keygen, public_key, agent_sha)
            if signer not in (None, fact["signer"]) or key not in (None, fact["signing_public_key"]) or marker not in (None, fact["qualification_marker"]):
                raise EvidenceError("fault action signer or marker differs")
            signer, key, marker = fact["signer"], fact["signing_public_key"], fact["qualification_marker"]
            raws[scenario], facts[scenario] = raw, fact
        if capacity["faults"]["spare_takeovers_recovered"] < 1 or capacity["faults"]["planned_restart_recoveries"] < 1 \
           or capacity["faults"]["controller_network_recoveries"] < 1 or capacity["metrics"]["websocket_reconnects"] < 1:
            raise EvidenceError("capacity evidence does not prove the three shared fault scenarios")
        output = {"$schema":"v1-fault-recovery-evidence.schema.json","schema_version":2,"status":"passed",
          "session_id":capacity["session_id"],"source":capacity["source"],"components":capacity["components"],
          "scenarios":{name:True for name in ("control_restart","desktop_reconnect","single_seat_replace","network_interruption","collection_retry","power_loss_recovery")},
          "ordinary_oj_isolation":{"errors":0,"restarts":0,"pid_changes":0},"signer":signer,
          "signing_public_key":key,
          "signing_public_key_sha256":hashlib.sha256(key.encode()).hexdigest(),
          "action_agent_sha256":{name:getattr(args,name + "_agent_sha256") for name in sorted(SCENARIOS)},
          "actions":{name:facts[name] for name in sorted(SCENARIOS)},
          "inputs":[{"name":"capacity","reference":args.capacity.name,"sha256":hashlib.sha256(capacity_raw).hexdigest()}] +
                   [{"name":name,"reference":getattr(args,name).name,"sha256":hashlib.sha256(canonical(facts[name])).hexdigest()} for name in sorted(SCENARIOS)]}
        validate_combined(
            output, capacity=capacity, capacity_raw=capacity_raw,
            ssh_keygen=args.ssh_keygen,
        )
        raw = (json.dumps(output, indent=2, sort_keys=True)+"\n").encode()
        output = Path(os.path.abspath(args.output)); parent = output.parent.resolve(strict=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".v1-fault-evidence-", dir=parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1; handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            os.link(temporary, output, follow_symlinks=False)
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if descriptor >= 0: os.close(descriptor)
            try: temporary.unlink()
            except FileNotFoundError: pass
        print(json.dumps({"status":"passed","sha256":hashlib.sha256(raw).hexdigest()},sort_keys=True)); return 0
    except (EvidenceError, CapacityError, OSError, subprocess.SubprocessError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2

if __name__ == "__main__": raise SystemExit(main())
