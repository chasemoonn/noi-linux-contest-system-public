#!/usr/bin/env python3
"""Collect one machine-verifiable clean-install rehearsal observation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
from types import SimpleNamespace
import stat
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import apply_v1_clean_install as clean
from orchestrator.services import install_transaction as transaction
import verify_v1_clean_install_rollback as rollback
import verify_v1_post_install as post_install
from build_v1_ordinary_oj_install_backup import collect as collect_ordinary
from verify_v1_install_backup import safe_directory, safe_file
from verify_v1_ordinary_oj_install_backup import (
    OrdinaryBackupError,
    compare as compare_ordinary,
    validate as validate_ordinary,
)


HEX64 = re.compile(r"^[a-f0-9]{64}$")
BASE_FILES = {"execution_log": "execution.log", "ordinary_after": "ordinary-after.json"}
SEALED_FILES = {"fresh_baseline": "fresh-baseline.json", "ordinary_before": "ordinary-before.json",
                "terminal_receipt": "terminal-receipt.json"}
POWER_FILES = {"ready_marker": "ready-marker.json", "child_log": "child.log", "resume_log": "resume.log"}


class ObservationError(RuntimeError):
    pass


def trusted_self() -> None:
    requested = Path(os.path.abspath(__file__)); metadata = os.lstat(requested)
    if requested != requested.resolve(strict=True) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ObservationError("clean install observation collector metadata is unsafe")
    if platform.system().lower() == "linux":
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ObservationError("clean install observation collector is not trusted")
        current = requested.parent
        while True:
            row = os.lstat(current)
            if stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode) or row.st_uid != 0 \
                    or stat.S_IMODE(row.st_mode) & 0o022:
                raise ObservationError("clean install observation collector ancestor is unsafe")
            if current.parent == current: break
            current = current.parent


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def fsync_directory(path: Path) -> None:
    if platform.system().lower() != "linux":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def atomic(path: Path, raw: bytes) -> None:
    if os.path.lexists(path):
        raise ObservationError("clean install rehearsal output already exists")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"): os.fchmod(descriptor, 0o600)
        else: os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1; output.write(raw); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path); fsync_directory(path.parent)
    finally:
        if descriptor >= 0: os.close(descriptor)
        if os.path.lexists(temporary): os.unlink(temporary)


def artifact(path: Path, *, maximum: int = 64 * 1024 * 1024) -> tuple[bytes, dict]:
    try: raw, metadata = safe_file(path, maximum=maximum)
    except (OSError, ValueError) as exc:
        raise ObservationError("clean install rehearsal artifact could not be read safely") from exc
    if platform.system().lower() == "linux" and (
            metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise ObservationError("clean install rehearsal artifact mode differs")
    return raw, {"filename": path.name, "bytes": len(raw),
                 "sha256": hashlib.sha256(raw).hexdigest()}


def copy_artifact(source: Path, target: Path, *, maximum: int = 64 * 1024 * 1024) -> dict:
    raw, _ = artifact(source, maximum=maximum)
    atomic(target, raw)
    _, record = artifact(target, maximum=maximum)
    return record


def execution_result(raw: bytes, row: dict, kind: str, phase: str | None,
                     power_artifacts: dict[str, tuple[bytes, dict]]) -> dict:
    lines = [line for line in raw.decode("utf-8").splitlines() if line]
    if len(lines) != 1:
        raise ObservationError("clean install rehearsal execution log framing differs")
    try: value = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ObservationError("clean install rehearsal execution log is invalid JSON") from exc
    common = {"status": "passed", "plan_id": row["plan_id"],
              "backup_manifest_sha256": row["backup_manifest_sha256"]}
    if kind == "success":
        expected = {**common, "mode": "success", "phase": None, "terminal": "committed"}
    elif kind == "phase_failure":
        expected = {**common, "mode": "phase-failure", "phase": phase,
                    "terminal": "rollback_verified"}
    else:
        expected = {**common, "kind": "power_loss", "phase": phase,
                    "terminal": "rollback_verified", "child_signal": "SIGKILL",
                    "ready_marker_sha256": power_artifacts["ready_marker"][1]["sha256"],
                    "child_log_sha256": power_artifacts["child_log"][1]["sha256"],
                    "resume_log_sha256": power_artifacts["resume_log"][1]["sha256"]}
    if value != expected:
        raise ObservationError("clean install rehearsal execution result differs")
    return value


def terminal(row: dict, kind: str, phase: str | None) -> tuple[bytes, dict]:
    status = "committed" if kind == "success" else "rollback_verified"
    directory = safe_directory(Path(row["transaction_directory"]))
    if os.path.lexists(directory / "service-install.pending.json"):
        raise ObservationError("clean install rehearsal left a pending transaction")
    path = directory / f"service-install.{status}-{row['plan_id']}.json"
    raw, _ = artifact(path, maximum=4 * 1024 * 1024)
    try: journal = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObservationError("clean install rehearsal terminal receipt is invalid") from exc
    validate_terminal_journal(journal, row, kind, phase)
    return raw, journal


def validate_terminal_journal(journal: dict, row: dict, kind: str, phase: str | None) -> dict:
    status = "committed" if kind == "success" else "rollback_verified"
    transaction.validate_journal(journal, row["plan_id"], row["backup_manifest_sha256"],
                                 transaction.CLEAN_PHASES, transaction.CLEAN_ROLLBACK_ORDER)
    if journal["status"] != status or journal["failure"] is not None and kind == "success":
        raise ObservationError("clean install rehearsal terminal status differs")
    if kind == "success":
        if journal["completed"] != list(transaction.CLEAN_PHASES) or journal["rollback_completed"]:
            raise ObservationError("clean install success phase coverage differs")
    else:
        expected_completed = list(transaction.CLEAN_PHASES[:transaction.CLEAN_PHASES.index(phase) + 1])
        expected_rollback = [name for name in transaction.CLEAN_ROLLBACK_ORDER if name in expected_completed]
        if journal["completed"] != expected_completed or journal["rollback_completed"] != expected_rollback:
            raise ObservationError("clean install rollback phase coverage differs")
        expected_failure = "InjectedPhaseFailure" if kind == "phase_failure" else "interrupted_apply"
        if journal["failure"] != expected_failure:
            raise ObservationError("clean install rollback failure boundary differs")
    return journal


def verify_live(row: dict, kind: str) -> None:
    if kind == "success":
        args = SimpleNamespace(backup_directory=Path(row["backup_directory"]),
            transaction_directory=Path(row["transaction_directory"]), plan_id=row["plan_id"],
            backup_manifest_sha256=row["backup_manifest_sha256"], source_release=row["source_release"],
            expected_contract=Path(row["expected_contract"]))
        result = post_install.verify(args)
        if result.get("status") != "verified" or result.get("closed") is not True \
                or result.get("cloud_closed") is not True or result.get("ordinary_oj_unchanged") is not True:
            raise ObservationError("clean install committed live verification differs")
    else:
        args = SimpleNamespace(backup_directory=Path(row["backup_directory"]), plan_id=row["plan_id"],
            backup_manifest_sha256=row["backup_manifest_sha256"], source_release=row["source_release"],
            expected_contract=Path(row["expected_contract"]),
            desired_controller_definition=Path(row["desired_controller_definition"]))
        expected = {"status": "rollback_verified", "plan_id": row["plan_id"],
                    "backup_manifest_sha256": row["backup_manifest_sha256"]}
        if rollback.verify(args) != expected:
            raise ObservationError("clean install rollback live verification differs")


def current_ordinary(row: dict) -> dict:
    expected = post_install.contract(Path(row["expected_contract"]), row["plan_id"], row["source_release"])
    return collect_ordinary(expected["oj_origin"], Path(expected["pm2_bin"]))


def collect(row: dict, private_plan_sha256: str, output: Path, kind: str,
            phase: str | None, *, live_verifier=verify_live, ordinary_collector=current_ordinary) -> dict:
    if row.get("scope") != "qualification-lab" or not HEX64.fullmatch(private_plan_sha256):
        raise ObservationError("clean install rehearsal plan identity differs")
    if kind not in {"success", "phase_failure", "power_loss"}:
        raise ObservationError("clean install rehearsal kind differs")
    if kind == "success" and phase is not None or kind != "success" and phase not in transaction.CLEAN_PHASES:
        raise ObservationError("clean install rehearsal phase differs")
    directory = safe_directory(output)
    expected_inputs = {BASE_FILES["execution_log"]} | (set(POWER_FILES.values()) if kind == "power_loss" else set())
    if {path.name for path in directory.iterdir()} != expected_inputs:
        raise ObservationError("clean install rehearsal input file set differs")

    baseline = Path(row["backup_directory"])
    baseline_raw, _ = artifact(baseline / "backup-manifest.json", maximum=4 * 1024 * 1024)
    if hashlib.sha256(baseline_raw).hexdigest() != row["backup_manifest_sha256"]:
        raise ObservationError("clean install rehearsal fresh baseline differs")
    ordinary_before_raw, _ = artifact(baseline / "ordinary-oj-before.json", maximum=4 * 1024 * 1024)
    observed_ordinary = validate_ordinary(ordinary_collector(row))
    atomic(directory / BASE_FILES["ordinary_after"], canonical(observed_ordinary))
    ordinary_after_raw, ordinary_after_record = artifact(directory / BASE_FILES["ordinary_after"], maximum=4 * 1024 * 1024)
    before = validate_ordinary(json.loads(ordinary_before_raw.decode("utf-8")))
    after = validate_ordinary(json.loads(ordinary_after_raw.decode("utf-8")))
    compare_ordinary(before, after)

    terminal_raw, _ = terminal(row, kind, phase)
    live_verifier(row, kind)
    execution_raw, execution_record = artifact(directory / BASE_FILES["execution_log"])
    power = {}
    for name, filename in POWER_FILES.items():
        if kind == "power_loss": power[name] = artifact(directory / filename)
    execution_result(execution_raw, row, kind, phase, power)

    records = {"execution_log": execution_record, "ordinary_after": ordinary_after_record,
        "fresh_baseline": copy_artifact(baseline / "backup-manifest.json", directory / SEALED_FILES["fresh_baseline"], maximum=4*1024*1024),
        "ordinary_before": copy_artifact(baseline / "ordinary-oj-before.json", directory / SEALED_FILES["ordinary_before"], maximum=4*1024*1024),
        "terminal_receipt": copy_artifact(Path(row["transaction_directory"]) /
            f"service-install.{'committed' if kind == 'success' else 'rollback_verified'}-{row['plan_id']}.json",
            directory / SEALED_FILES["terminal_receipt"], maximum=4*1024*1024)}
    for name, (_raw, record) in power.items(): records[name] = record
    result = {"terminal": "committed" if kind == "success" else "rollback_verified"}
    if kind == "success":
        result.update({"controller_healthy": True, "closed_frontend": True, "active_seats": 0,
            "managed_rules": 0, "cloud_state": "STOPPED", "pending_markers": 0,
            "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0})
    else:
        result.update({"clean_target": True, "caddy_restored": True, "hydro_restored": True,
            "controller_absent": True, "cloud_state": "STOPPED", "pending_markers": 0,
            "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0})
    document = {"$schema": "v1-clean-install-rehearsal-observation.schema.json", "schema_version": 1,
        "plan_id": row["plan_id"], "private_plan_sha256": private_plan_sha256,
        "backup_manifest_sha256": row["backup_manifest_sha256"], "kind": kind, "phase": phase,
        "result": result, "artifacts": records}
    atomic(directory / "observation.json", canonical(document))
    return validate_document(document, directory, row)


def exact(value, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ObservationError(f"{label} field set differs")
    return value


def validate_document(value: dict, directory: Path, row: dict) -> dict:
    document = exact(value, {"$schema", "schema_version", "plan_id", "private_plan_sha256",
        "backup_manifest_sha256", "kind", "phase", "result", "artifacts"}, "observation")
    if document["$schema"] != "v1-clean-install-rehearsal-observation.schema.json" \
            or document["schema_version"] != 1 or document["plan_id"] != row["plan_id"] \
            or document["backup_manifest_sha256"] != row["backup_manifest_sha256"] \
            or not HEX64.fullmatch(str(document["private_plan_sha256"])):
        raise ObservationError("clean install rehearsal observation identity differs")
    kind, phase = document["kind"], document["phase"]
    if kind not in {"success", "phase_failure", "power_loss"} \
            or kind == "success" and phase is not None \
            or kind != "success" and phase not in transaction.CLEAN_PHASES:
        raise ObservationError("clean install rehearsal observation scenario differs")
    success_keys = {"terminal", "controller_healthy", "closed_frontend", "active_seats",
        "managed_rules", "cloud_state", "pending_markers", "ordinary_oj_errors",
        "ordinary_oj_restarts", "ordinary_oj_pid_changes"}
    rollback_keys = {"terminal", "clean_target", "caddy_restored", "hydro_restored",
        "controller_absent", "cloud_state", "pending_markers", "ordinary_oj_errors",
        "ordinary_oj_restarts", "ordinary_oj_pid_changes"}
    result = exact(document["result"], success_keys if kind == "success" else rollback_keys, "result")
    if kind == "success":
        if result != {"terminal": "committed", "controller_healthy": True,
                "closed_frontend": True, "active_seats": 0, "managed_rules": 0,
                "cloud_state": "STOPPED", "pending_markers": 0, "ordinary_oj_errors": 0,
                "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0}:
            raise ObservationError("clean install success observation differs")
    elif result != {"terminal": "rollback_verified", "clean_target": True,
            "caddy_restored": True, "hydro_restored": True, "controller_absent": True,
            "cloud_state": "STOPPED", "pending_markers": 0, "ordinary_oj_errors": 0,
            "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0}:
        raise ObservationError("clean install rollback observation differs")

    artifact_names = set(BASE_FILES) | set(SEALED_FILES) | (set(POWER_FILES) if kind == "power_loss" else set())
    artifacts = exact(document["artifacts"], artifact_names, "artifacts")
    expected_files = {"observation.json"}
    loaded = {}
    for name, record_value in artifacts.items():
        record = exact(record_value, {"filename", "bytes", "sha256"}, f"artifact {name}")
        expected_name = {**BASE_FILES, **SEALED_FILES, **POWER_FILES}[name]
        if record["filename"] != expected_name or isinstance(record["bytes"], bool) \
                or not isinstance(record["bytes"], int) or record["bytes"] < 0 \
                or not HEX64.fullmatch(str(record["sha256"])):
            raise ObservationError("clean install rehearsal artifact record differs")
        raw, observed = artifact(directory / expected_name)
        if observed != record:
            raise ObservationError("clean install rehearsal artifact bytes differ")
        expected_files.add(expected_name); loaded[name] = raw
    if {path.name for path in directory.iterdir()} != expected_files:
        raise ObservationError("clean install rehearsal sealed file set differs")
    if hashlib.sha256(loaded["fresh_baseline"]).hexdigest() != document["backup_manifest_sha256"]:
        raise ObservationError("clean install rehearsal sealed baseline differs")
    before = validate_ordinary(json.loads(loaded["ordinary_before"].decode("utf-8")))
    after = validate_ordinary(json.loads(loaded["ordinary_after"].decode("utf-8")))
    compare_ordinary(before, after)
    try: journal = json.loads(loaded["terminal_receipt"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObservationError("clean install rehearsal copied terminal is invalid") from exc
    validate_terminal_journal(journal, row, kind, phase)
    power = {name: (loaded[name], artifacts[name]) for name in POWER_FILES if name in loaded}
    execution_result(loaded["execution_log"], row, kind, phase, power)
    return document


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--private-plan", required=True, type=Path)
    parser.add_argument("--expected-plan-sha256", required=True); parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--kind", required=True, choices=("success", "phase_failure", "power_loss"))
    parser.add_argument("--phase", choices=transaction.CLEAN_PHASES); args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise ObservationError("clean install rehearsal observation requires Linux root")
        trusted_self(); clean.trusted_self(); row = clean.load_plan(args.private_plan, args.expected_plan_sha256)
        value = collect(row, args.expected_plan_sha256, args.output_directory, args.kind, args.phase)
        print(json.dumps({"status": "sealed", "kind": value["kind"], "phase": value["phase"],
                          "observation_sha256": hashlib.sha256(canonical(value)).hexdigest()}, sort_keys=True)); return 0
    except (ObservationError, clean.ApplyInstallError, rollback.CleanRollbackError, post_install.PostInstallError,
            transaction.InstallTransactionError, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
