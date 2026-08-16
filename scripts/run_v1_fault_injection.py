#!/usr/bin/env python3
"""Run the deterministic NOI Linux V1 submission fault-injection gate."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {
    "response_lost_after_commit": (
        ROOT / "hydro-plugin-orchestrator/tests/orchestrator-submit.test.js",
        "response loss and plugin restart",
    ),
    "plugin_restart_and_concurrent_resolution": (
        ROOT / "hydro-plugin-orchestrator/tests/orchestrator-submit.test.js",
        "const [resolved, concurrent] = await Promise.all",
    ),
    "controller_restart": (
        ROOT / "orchestrator/tests/test_realtime_judge.py",
        "test_controller_restart_resolves_persisted_ambiguous_submission",
    ),
    "resolution_network_failure": (
        ROOT / "orchestrator/tests/test_realtime_judge.py",
        "test_resolution_network_failure_survives_restart_without_replay",
    ),
    "concurrent_sqlite_claim": (
        ROOT / "orchestrator/tests/test_store.py",
        "test_ambiguous_resolution_claim_is_single_owner_across_store_connections",
    ),
}


class GateError(RuntimeError):
    pass


def require_scenarios() -> None:
    for scenario, (path, marker) in SCENARIOS.items():
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GateError(f"cannot read {path.relative_to(ROOT)}: {exc}") from exc
        if marker not in content:
            raise GateError(f"scenario is missing: {scenario}")


def run(label: str, command: list[str], cwd: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "orchestrator")
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(f"{label} could not complete: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if len(detail) > 4000:
            detail = detail[-4000:]
        raise GateError(f"{label} failed with exit {result.returncode}:\n{detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--require-linux",
        action="store_true",
        help="fail unless this gate is running on Linux",
    )
    args = parser.parse_args()
    try:
        system = platform.system().lower()
        if args.require_linux and system != "linux":
            raise GateError("Linux is required for this release gate")
        require_scenarios()
        commands = [
            (
                "Hydro plugin submission faults",
                ["node", "--test", "tests/orchestrator-submit.test.js"],
                ROOT / "hydro-plugin-orchestrator",
            ),
            (
                "Hydro submit transport contract",
                [sys.executable, "tests/test_hydro_submit.py", "-v"],
                ROOT / "orchestrator",
            ),
            (
                "controller restart and resolution faults",
                [sys.executable, "tests/test_realtime_judge.py", "-v"],
                ROOT / "orchestrator",
            ),
            (
                "SQLite outbox concurrency faults",
                [sys.executable, "tests/test_store.py", "-v"],
                ROOT / "orchestrator",
            ),
        ]
        for label, command, cwd in commands:
            run(label, command, cwd)
        print(
            json.dumps(
                {
                    "commands": len(commands),
                    "platform": system,
                    "scenarios": sorted(SCENARIOS),
                    "status": "passed",
                },
                sort_keys=True,
            )
        )
        return 0
    except GateError as exc:
        print(f"FAULT_INJECTION_FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
