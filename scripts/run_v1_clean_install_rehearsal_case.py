#!/usr/bin/env python3
"""Run and seal one clean-install rehearsal case on a fresh lab snapshot."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import apply_v1_clean_install as clean
import collect_v1_clean_install_rehearsal_observation as observation
from orchestrator.services import install_transaction as transaction
import run_v1_clean_install_power_loss_supervisor as power
from verify_v1_install_backup import safe_directory


class CaseError(RuntimeError):
    pass


def private_case_directory(path: Path) -> Path:
    requested = Path(os.path.abspath(path)); parent = safe_directory(requested.parent)
    if os.path.lexists(requested):
        raise CaseError("clean install rehearsal case directory already exists")
    os.mkdir(requested, 0o700); os.chmod(requested, 0o700); observation.fsync_directory(parent)
    return safe_directory(requested)


def trusted_self() -> None:
    requested = Path(os.path.abspath(__file__)); metadata = os.lstat(requested)
    if requested != requested.resolve(strict=True) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CaseError("clean install rehearsal case runner metadata is unsafe")
    if platform.system().lower() == "linux":
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise CaseError("clean install rehearsal case runner is not trusted")
        current = requested.parent
        while True:
            row = os.lstat(current)
            if stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode) or row.st_uid != 0 \
                    or stat.S_IMODE(row.st_mode) & 0o022:
                raise CaseError("clean install rehearsal case runner ancestor is unsafe")
            if current.parent == current: break
            current = current.parent


def run_logged(command: list[str], log_path: Path, timeout_seconds: int, *,
               popen=subprocess.Popen, contain=power.contain_process_group) -> tuple[int, bool]:
    child = None; timed_out = False
    with power.create_log(log_path) as log:
        try:
            child = popen(command, cwd=str(ROOT), env=power.clean_environment(), stdout=log,
                          stderr=subprocess.STDOUT, start_new_session=True)
            try: returncode = child.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True; contain(child.pid); returncode = child.wait(timeout=10)
        finally:
            if child is not None: contain(child.pid)
            log.flush(); os.fsync(log.fileno())
    return returncode, timed_out


def recover_interrupted(row: dict, private_plan: Path, expected_plan_sha256: str,
                        output: Path, timeout_seconds: int, *, popen=subprocess.Popen,
                        contain=power.contain_process_group) -> None:
    transaction_directory = safe_directory(Path(row["transaction_directory"]))
    pending = transaction_directory / "service-install.pending.json"
    rolled_back = transaction_directory / f"service-install.rollback_verified-{row['plan_id']}.json"
    committed = transaction_directory / f"service-install.committed-{row['plan_id']}.json"
    if os.path.lexists(committed):
        raise CaseError("failed rehearsal process reached an irreversible committed terminal")
    if os.path.lexists(rolled_back) and not os.path.lexists(pending):
        return
    if not os.path.lexists(pending):
        raise CaseError("failed rehearsal process left no resumable transaction")
    command = power.command(Path(row["executables"]["python"]), private_plan,
                            expected_plan_sha256, "resume")
    returncode, timed_out = run_logged(command, output / "failure-resume.log", timeout_seconds,
                                       popen=popen, contain=contain)
    if timed_out or returncode != 0:
        raise CaseError("failed rehearsal process could not be rolled back")
    power.final_json(output / "failure-resume.log", row)
    if os.path.lexists(pending) or not os.path.lexists(rolled_back):
        raise CaseError("failed rehearsal process did not seal its rollback terminal")
    try: journal = json.loads(observation.artifact(rolled_back, maximum=4 * 1024 * 1024)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaseError("failed rehearsal rollback terminal is invalid") from exc
    transaction.validate_journal(journal, row["plan_id"], row["backup_manifest_sha256"],
                                 transaction.CLEAN_PHASES, transaction.CLEAN_ROLLBACK_ORDER)
    scope = set(journal["completed"])
    expected_rollback = [name for name in transaction.CLEAN_ROLLBACK_ORDER if name in scope]
    if journal["status"] != "rollback_verified" or journal["rollback_completed"] != expected_rollback:
        raise CaseError("failed rehearsal rollback terminal differs")


def execute(row: dict, private_plan: Path, expected_plan_sha256: str, kind: str,
            phase: str | None, output: Path, timeout_seconds: int, *,
            popen=subprocess.Popen, contain=power.contain_process_group,
            power_supervise=power.supervise,
            collector=observation.collect) -> dict:
    if row.get("scope") != "qualification-lab" or kind not in {"success", "phase_failure", "power_loss"}:
        raise CaseError("clean install rehearsal case scope differs")
    if kind == "success" and phase is not None or kind != "success" and phase not in transaction.CLEAN_PHASES:
        raise CaseError("clean install rehearsal case phase differs")
    if timeout_seconds < 10 or timeout_seconds > 3600:
        raise CaseError("clean install rehearsal case timeout differs")
    directory = private_case_directory(output)
    execution_log = directory / "execution.log"
    if kind == "power_loss":
        result = power_supervise(row, private_plan, expected_plan_sha256, phase,
            directory / "ready-marker.json", directory / "child.log", directory / "resume.log",
            timeout_seconds)
        observation.atomic(execution_log, observation.canonical(result))
    else:
        mode = "success" if kind == "success" else "phase-failure"
        command = power.command(Path(row["executables"]["python"]), private_plan,
                                expected_plan_sha256, mode, phase=phase)
        returncode, timed_out = run_logged(command, execution_log, timeout_seconds,
                                           popen=popen, contain=contain)
        if timed_out or returncode != 0:
            recover_interrupted(row, private_plan, expected_plan_sha256, directory, timeout_seconds,
                                popen=popen, contain=contain)
            raise CaseError("clean install rehearsal scenario did not reach its expected terminal")
    sealed = collector(row, expected_plan_sha256, directory, kind, phase)
    return {"status": "sealed", "kind": sealed["kind"], "phase": sealed["phase"],
            "plan_id": sealed["plan_id"],
            "observation_sha256": __import__("hashlib").sha256(observation.canonical(sealed)).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--private-plan", required=True, type=Path)
    parser.add_argument("--expected-plan-sha256", required=True); parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--kind", required=True, choices=("success", "phase_failure", "power_loss"))
    parser.add_argument("--phase", choices=transaction.CLEAN_PHASES); parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise CaseError("clean install rehearsal case requires Linux root")
        trusted_self(); clean.trusted_self(); row = clean.load_plan(args.private_plan, args.expected_plan_sha256)
        print(json.dumps(execute(row, args.private_plan, args.expected_plan_sha256, args.kind,
                                 args.phase, args.output_directory, args.timeout_seconds), sort_keys=True)); return 0
    except (CaseError, observation.ObservationError, power.PowerLossSupervisorError,
            clean.ApplyInstallError, transaction.InstallTransactionError, OSError, ValueError,
            UnicodeDecodeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
