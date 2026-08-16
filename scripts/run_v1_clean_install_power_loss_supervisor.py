#!/usr/bin/env python3
"""Supervise one real SIGKILL clean-install qualification scenario."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import stat
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import apply_v1_clean_install as clean
from orchestrator.services import install_transaction as transaction
from verify_v1_install_backup import safe_directory, safe_file


SIGKILL = getattr(signal, "SIGKILL", 9)
SCENARIO = ROOT / "scripts" / "run_v1_clean_install_rehearsal_scenario.py"


class PowerLossSupervisorError(RuntimeError):
    pass


def trusted_self() -> None:
    requested = Path(os.path.abspath(__file__))
    metadata = os.lstat(requested)
    if requested != requested.resolve(strict=True) or not stat.S_ISREG(metadata.st_mode) \
            or metadata.st_nlink != 1:
        raise PowerLossSupervisorError("power-loss supervisor metadata is unsafe")
    if platform.system().lower() == "linux":
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise PowerLossSupervisorError("power-loss supervisor is not trusted")
        current = requested.parent
        while True:
            row = os.lstat(current)
            if stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode) or row.st_uid != 0 \
                    or stat.S_IMODE(row.st_mode) & 0o022:
                raise PowerLossSupervisorError("power-loss supervisor ancestor is unsafe")
            if current.parent == current:
                break
            current = current.parent


def create_log(path: Path):
    requested = Path(os.path.abspath(path))
    safe_directory(requested.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(requested, flags, 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 \
            or (platform.system().lower() == "linux" and
                (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600)):
        os.close(descriptor)
        raise PowerLossSupervisorError("power-loss log metadata is unsafe")
    return os.fdopen(descriptor, "wb", closefd=True)


def marker(path: Path, row: dict, phase: str, pid: int) -> dict:
    raw, metadata = safe_file(path, maximum=64 * 1024)
    if platform.system().lower() == "linux" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PowerLossSupervisorError("power-loss ready marker mode differs")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PowerLossSupervisorError("power-loss ready marker is invalid JSON") from exc
    expected = {"schema_version": 1, "plan_id": row["plan_id"], "mode": "power_loss",
                "phase": phase, "pid": pid}
    if value != expected:
        raise PowerLossSupervisorError("power-loss ready marker identity differs")
    return value


def final_json(path: Path, row: dict) -> dict:
    raw, _ = safe_file(path, maximum=4 * 1024 * 1024)
    lines = [line for line in raw.decode("utf-8").splitlines() if line]
    if len(lines) != 1:
        raise PowerLossSupervisorError("power-loss resume log framing differs")
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise PowerLossSupervisorError("power-loss resume result is invalid JSON") from exc
    expected = {"status": "passed", "mode": "resume", "phase": None,
                "terminal": "rollback_verified", "plan_id": row["plan_id"],
                "backup_manifest_sha256": row["backup_manifest_sha256"]}
    if value != expected:
        raise PowerLossSupervisorError("power-loss resume result differs")
    return value


def command(python: Path, plan: Path, expected_sha256: str, mode: str, *,
            phase: str | None = None, ready: Path | None = None) -> list[str]:
    result = [str(python), str(SCENARIO), "--private-plan", str(plan),
              "--expected-plan-sha256", expected_sha256, "--mode", mode]
    if phase is not None:
        result += ["--phase", phase]
    if ready is not None:
        result += ["--ready-marker", str(ready)]
    return result


def clean_environment() -> dict[str, str]:
    return {"HOME": "/root", "USER": "root", "LOGNAME": "root", "SHELL": "/bin/bash",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def contain_process_group(pid: int) -> None:
    try:
        os.killpg(pid, SIGKILL)
    except ProcessLookupError:
        return
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise


def supervise(row: dict, private_plan: Path, expected_plan_sha256: str, phase: str,
              ready: Path, child_log: Path, resume_log: Path, timeout_seconds: int, *,
              popen=subprocess.Popen, resume_popen=subprocess.Popen, monotonic=time.monotonic,
              sleep=time.sleep, contain=contain_process_group) -> dict:
    if row.get("scope") != "qualification-lab" or phase not in transaction.CLEAN_PHASES:
        raise PowerLossSupervisorError("power-loss qualification scope differs")
    if timeout_seconds < 10 or timeout_seconds > 3600:
        raise PowerLossSupervisorError("power-loss supervisor timeout differs")
    for path in (ready, child_log, resume_log):
        safe_directory(Path(os.path.abspath(path)).parent)
        if os.path.lexists(path):
            raise PowerLossSupervisorError("power-loss output already exists")
    python = Path(row["executables"]["python"])
    child_args = command(python, private_plan, expected_plan_sha256,
                         "power-loss-child", phase=phase, ready=ready)
    anomaly = None
    child = None
    with create_log(child_log) as output:
        try:
            child = popen(child_args, cwd=str(ROOT), env=clean_environment(), stdout=output,
                          stderr=subprocess.STDOUT, start_new_session=True)
            deadline = monotonic() + timeout_seconds
            while monotonic() < deadline and not os.path.lexists(ready):
                if child.poll() is not None:
                    break
                sleep(0.1)
            if not os.path.lexists(ready):
                anomaly = "power-loss child did not publish its durable boundary"
            else:
                try:
                    marker(ready, row, phase, child.pid)
                except (PowerLossSupervisorError, OSError, ValueError,
                        UnicodeDecodeError, json.JSONDecodeError):
                    anomaly = "power-loss ready marker verification failed"
            try:
                returncode = child.wait(timeout=max(1, min(30, timeout_seconds)))
            except subprocess.TimeoutExpired:
                anomaly = anomaly or "power-loss child did not terminate after its durable boundary"
                contain(child.pid)
                returncode = child.wait(timeout=10)
            if returncode != -SIGKILL:
                anomaly = anomaly or "power-loss child was not terminated by SIGKILL"
        except (OSError, subprocess.SubprocessError):
            anomaly = anomaly or "power-loss child supervision failed"
        finally:
            if child is not None:
                contain(child.pid)
            output.flush(); os.fsync(output.fileno())

    transaction_directory = safe_directory(Path(row["transaction_directory"]))
    pending = transaction_directory / "service-install.pending.json"
    committed = transaction_directory / f"service-install.committed-{row['plan_id']}.json"
    rolled_back = transaction_directory / f"service-install.rollback_verified-{row['plan_id']}.json"
    if os.path.lexists(committed):
        raise PowerLossSupervisorError("power-loss child reached an irreversible committed terminal")
    if not os.path.lexists(pending) and not os.path.lexists(rolled_back):
        raise PowerLossSupervisorError("power-loss child left no resumable transaction")
    resume_args = command(python, private_plan, expected_plan_sha256, "resume")
    resume_child = None; resume_timed_out = False
    with create_log(resume_log) as output:
        try:
            resume_child = resume_popen(resume_args, cwd=str(ROOT), env=clean_environment(), stdout=output,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            try: resume_returncode = resume_child.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                resume_timed_out = True; contain(resume_child.pid)
                resume_returncode = resume_child.wait(timeout=10)
        finally:
            if resume_child is not None: contain(resume_child.pid)
            output.flush(); os.fsync(output.fileno())
    if resume_timed_out or resume_returncode != 0:
        raise PowerLossSupervisorError("power-loss resume failed to reach a terminal rollback")
    final_json(resume_log, row)
    if anomaly is not None:
        raise PowerLossSupervisorError(anomaly)
    child_raw, _ = safe_file(child_log, maximum=64 * 1024 * 1024)
    resume_raw, _ = safe_file(resume_log, maximum=64 * 1024 * 1024)
    ready_raw, _ = safe_file(ready, maximum=64 * 1024)
    return {"status": "passed", "kind": "power_loss", "phase": phase,
            "terminal": "rollback_verified", "plan_id": row["plan_id"],
            "backup_manifest_sha256": row["backup_manifest_sha256"],
            "child_signal": "SIGKILL", "ready_marker_sha256": hashlib.sha256(ready_raw).hexdigest(),
            "child_log_sha256": hashlib.sha256(child_raw).hexdigest(),
            "resume_log_sha256": hashlib.sha256(resume_raw).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-plan", required=True, type=Path)
    parser.add_argument("--expected-plan-sha256", required=True)
    parser.add_argument("--phase", required=True, choices=transaction.CLEAN_PHASES)
    parser.add_argument("--ready-marker", required=True, type=Path)
    parser.add_argument("--child-log", required=True, type=Path)
    parser.add_argument("--resume-log", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise PowerLossSupervisorError("power-loss supervisor requires Linux root")
        trusted_self(); clean.trusted_self()
        row = clean.load_plan(args.private_plan, args.expected_plan_sha256)
        result = supervise(row, args.private_plan, args.expected_plan_sha256, args.phase,
                           args.ready_marker, args.child_log, args.resume_log, args.timeout_seconds)
        print(json.dumps(result, sort_keys=True)); return 0
    except (PowerLossSupervisorError, clean.ApplyInstallError, transaction.InstallTransactionError,
            OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
