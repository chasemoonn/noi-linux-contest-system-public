#!/usr/bin/env python3
"""Verify downloaded fixture evidence, without claiming production qualification."""
import argparse
import hashlib
import json
from pathlib import Path
import re

CASES = ("sigterm", "systemctl-stop", "stop-transaction", "sigkill", "watchdog-sigstop", "expiry")
SOURCES = ("deploy/maintenance/maintenance_guard.py", "deploy/maintenance/noi-maintenance-guard.service",
           "scripts/maintenance_systemd_ci_fixture.py", "scripts/run_maintenance_systemd_ci.py")


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def verify(directory, source_root, revision):
    value = json.loads((directory / "evidence.json").read_text())
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None and value.get("source_revision") == revision, "revision mismatch")
    require(type(value.get("schema_version")) is int and value["schema_version"] == 1, "schema mismatch")
    require(value.get("passed") is True and "failure" not in value, "failed or incomplete run")
    require(value.get("environment", {}).get("os") == "ubuntu-22.04" and value["environment"].get("pid1") == "systemd", "not real Jammy systemd")
    require(value.get("policy_unchanged") is True and value.get("policy_before") == value.get("policy_after")
            and bool(value.get("policy_before")), "update policy changed or missing")
    require(set(value.get("sources", {})) == set(SOURCES), "source evidence missing")
    for name in SOURCES:
        require(hashlib.sha256((source_root / name).read_bytes()).hexdigest() == value["sources"][name], "source SHA mismatch: " + name)
    require([item.get("name") for item in value.get("scenarios", [])] == list(CASES), "missing, duplicate or reordered cases")
    for item in value["scenarios"]:
        name = item["name"]
        require(item.get("passed") is True and item.get("fixture_units_removed") is True, name + ": failure or incomplete cleanup")
        ready, ended = item["ready_state"], item["end_state"]
        require(ready["ActiveState"] == "active" and int(ready["MainPID"]) > 1
                and ready["Type"] == "notify" and ready["WatchdogUSec"] == "15s"
                and ready["RuntimeMaxUSec"] == "4h" and ready["Restart"] == "no", name + ": supervisor mismatch")
        require(ended["ActiveState"] in ("inactive", "failed") and ended["ControlPID"] == "0"
                and ended["MainPID"] == "0" and ended["NRestarts"] == "0", name + ": daemon not ended")
        require(item["apt_busy_while_protected"] == [True] * 4 and item["apt_busy_after_release"] == [False] * 4, name + ": APT lock mismatch")
        if name in ("sigterm", "systemctl-stop", "stop-transaction", "expiry"):
            require(item["gateway_closure"]["locks_busy"] == [True] * 4, name + ": unlock preceded closure")
        expected_result = {"sigkill": "signal", "watchdog-sigstop": "watchdog", "expiry": "exit-code"}.get(name, "success")
        require(ended["Result"] == expected_result, name + ": wrong exit cause")
        bound = 165 if name == "expiry" else 45 if name in ("sigkill", "watchdog-sigstop") else 10
        require(0 <= item["end_after_action_seconds"] < bound, name + ": time bound exceeded")
        if name == "expiry":
            require(item["total_seconds"] >= 140, "actual expiry wait missing")
        journal = (directory / (name + ".journal.txt")).read_bytes()
        require(hashlib.sha256(journal).hexdigest() == item["journal_sha256"], name + ": journal SHA mismatch")
        require(b'"event": "protected"' in journal and b'"event": "systemd_close_confirmed"' in journal, name + ": lifecycle evidence missing")
    require(len(value.get("limitations", [])) >= 4, "fixture limits omitted")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    verify(args.directory, args.source_root, args.expected_revision)
    print("SYSTEMD_FIXTURE_EVIDENCE_OK cases=6 production_qualified=false")
