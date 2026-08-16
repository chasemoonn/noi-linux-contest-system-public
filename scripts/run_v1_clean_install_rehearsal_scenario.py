#!/usr/bin/env python3
"""Qualification-only entrypoint for one clean-install matrix scenario."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import signal
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import apply_v1_clean_install as clean
from orchestrator.services import install_transaction as transaction
from verify_v1_install_backup import safe_directory


class ScenarioError(RuntimeError):
    pass


class InjectedPhaseFailure(RuntimeError):
    pass


SIGKILL = getattr(signal, "SIGKILL", 9)


def ready_marker(path: Path, row: dict, phase: str) -> None:
    requested = Path(os.path.abspath(path))
    safe_directory(requested.parent)
    if os.path.lexists(requested):
        raise ScenarioError("power-loss ready marker already exists")
    transaction._atomic_json(requested, {"schema_version": 1, "plan_id": row["plan_id"],
        "mode": "power_loss", "phase": phase, "pid": os.getpid()})


def execute(row: dict, mode: str, phase: str | None, marker: Path | None = None,
            *, kill_process=os.kill) -> dict:
    if row.get("scope") != "qualification-lab":
        raise ScenarioError("clean install rehearsal requires a qualification-lab plan")
    if mode not in {"success", "phase-failure", "power-loss-child", "resume"}:
        raise ScenarioError("clean install rehearsal mode differs")
    if mode in {"phase-failure", "power-loss-child"}:
        if phase not in transaction.CLEAN_PHASES:
            raise ScenarioError("clean install rehearsal phase differs")
    elif phase is not None:
        raise ScenarioError("clean install rehearsal phase is unexpected")
    if mode == "power-loss-child" and marker is None:
        raise ScenarioError("power-loss ready marker is required")
    if mode != "power-loss-child" and marker is not None:
        raise ScenarioError("power-loss ready marker is unexpected")

    contract = clean.verify_bindings(row); drivers, final = clean.drivers(row, contract)
    hook = None
    if mode == "phase-failure":
        def hook(_context, current, _receipt):
            if current == phase:
                raise InjectedPhaseFailure("qualification phase failure")
    elif mode == "power-loss-child":
        def hook(_context, current, _receipt):
            if current == phase:
                ready_marker(marker, row, current)
                kill_process(os.getpid(), SIGKILL)
                raise ScenarioError("power-loss kill unexpectedly returned")
    result = transaction.run_clean(Path(row["transaction_directory"]), row["plan_id"],
        row["backup_manifest_sha256"], drivers, final, after_phase_committed=hook)
    expected = "committed" if mode == "success" else "rollback_verified"
    if result.get("status") != expected:
        raise ScenarioError("clean install rehearsal terminal differs")
    return {"status": "passed", "mode": mode, "phase": phase, "terminal": expected,
            "plan_id": row["plan_id"], "backup_manifest_sha256": row["backup_manifest_sha256"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-plan", required=True, type=Path)
    parser.add_argument("--expected-plan-sha256", required=True)
    parser.add_argument("--mode", required=True, choices=("success", "phase-failure", "power-loss-child", "resume"))
    parser.add_argument("--phase", choices=transaction.CLEAN_PHASES)
    parser.add_argument("--ready-marker", type=Path)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise ScenarioError("clean install rehearsal requires Linux root")
        clean.trusted_self(); row = clean.load_plan(args.private_plan, args.expected_plan_sha256)
        print(json.dumps(execute(row, args.mode, args.phase, args.ready_marker), sort_keys=True)); return 0
    except (ScenarioError, clean.ApplyInstallError, transaction.InstallTransactionError, OSError, ValueError,
            UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
