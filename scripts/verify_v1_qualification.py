#!/usr/bin/env python3
"""Validate an NOI Linux V1 qualification report without external packages."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


PROFILE = "aliyun-hydro5-pm2-direct-v1"
HEX40 = re.compile(r"^[a-f0-9]{40}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
EVIDENCE_NAMES = {
    "linux_ci",
    "cross_machine_import_rollback",
    "independent_teacher_install",
    "single_seat",
    "fault_recovery",
    "ordinary_oj_isolation",
    "capacity_15_plus_2",
}
SINGLE_SEAT_CHECKS = {
    "materials",
    "desktop",
    "compile",
    "manual_submit",
    "cutoff_submit",
    "oj_record",
    "collection",
    "shutdown",
    "test_cleanup",
}
FAULT_SCENARIOS = {
    "control_restart",
    "desktop_reconnect",
    "single_seat_replace",
    "network_interruption",
    "collection_retry",
    "power_loss_recovery",
}


class ReportError(ValueError):
    pass


def exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReportError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise ReportError(
            f"{label} keys differ: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )
    return value


def require_pattern(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ReportError(f"{label} has an invalid value")
    return value


def require_nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportError(f"{label} must be a non-empty string")
    return value


def require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportError(f"{label} must be a non-negative integer")
    return value


def validate_basic_evidence(value: Any, label: str, extra: set[str]) -> dict[str, Any]:
    row = exact_keys(
        value,
        {"status", "reference", "evidence_sha256"} | extra,
        label,
    )
    if row["status"] not in {"passed", "pending", "failed"}:
        raise ReportError(f"{label}.status is invalid")
    if row["status"] == "passed":
        require_nonempty(row["reference"], f"{label}.reference")
        require_pattern(row["evidence_sha256"], HEX64, f"{label}.evidence_sha256")
    else:
        if row["reference"] is not None and not isinstance(row["reference"], str):
            raise ReportError(f"{label}.reference must be a string or null")
        if row["evidence_sha256"] is not None:
            require_pattern(row["evidence_sha256"], HEX64, f"{label}.evidence_sha256")
    return row


def validate_report(document: Any, *, require_qualified: bool = False) -> dict[str, Any]:
    report = exact_keys(
        document,
        {
            "$schema",
            "schema_version",
            "source_revision",
            "profile",
            "components",
            "evidence",
            "reviewers",
            "production_qualified",
        },
        "report",
    )
    if report["$schema"] != "v1-qualification-report.schema.json":
        raise ReportError("unexpected report schema")
    if report["schema_version"] != 2:
        raise ReportError("unsupported report schema version")
    revision = require_pattern(report["source_revision"], HEX40, "source_revision")
    if report["profile"] != PROFILE:
        raise ReportError("unsupported profile")

    components = exact_keys(
        report["components"],
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
    desktop_revision = require_pattern(
        components["desktop_source_revision"], HEX40, "desktop source revision"
    )
    if desktop_revision != revision:
        raise ReportError("desktop source revision differs from report revision")
    require_pattern(components["hydro_plugin_sha256"], HEX64, "Hydro plugin SHA256")

    evidence = exact_keys(report["evidence"], EVIDENCE_NAMES, "evidence")
    basic = {}
    for name in ("linux_ci", "cross_machine_import_rollback", "independent_teacher_install"):
        basic[name] = validate_basic_evidence(evidence[name], f"evidence.{name}", set())
    fixed_basic_references = {
        "linux_ci": "v1-linux-ci-evidence.json",
        "cross_machine_import_rollback": "v1-cross-machine-image-evidence.json",
        "independent_teacher_install": "v1-independent-teacher-install-evidence.json",
    }
    for name, reference in fixed_basic_references.items():
        if basic[name]["status"] == "passed" and basic[name]["reference"] != reference:
            raise ReportError(f"passed {name} evidence must reference {reference}")
    single = validate_basic_evidence(
        evidence["single_seat"], "evidence.single_seat", {"checks"}
    )
    fault = validate_basic_evidence(
        evidence["fault_recovery"], "evidence.fault_recovery", {"scenarios"}
    )
    isolation = validate_basic_evidence(
        evidence["ordinary_oj_isolation"],
        "evidence.ordinary_oj_isolation",
        {"ordinary_oj_errors", "ordinary_oj_restarts", "ordinary_oj_pid_changes"},
    )
    capacity = validate_basic_evidence(
        evidence["capacity_15_plus_2"],
        "evidence.capacity_15_plus_2",
        {
            "formal_seats",
            "spare_seats",
            "duration_seconds",
            "unexpected_seat_restarts",
            "failed_submissions",
            "failed_collections",
            "verified_seats",
            "spare_takeovers",
            "planned_restart_recoveries",
            "controller_network_recoveries",
            "capacity_margin_accepted",
        },
    )

    statuses = [row["status"] for row in [*basic.values(), single, fault, isolation, capacity]]
    all_passed = all(status == "passed" for status in statuses)
    if single["status"] == "passed":
        if single["reference"] != "single-seat-evidence.json":
            raise ReportError(
                "passed single-seat evidence must reference single-seat-evidence.json"
            )
        checks = exact_keys(single["checks"], SINGLE_SEAT_CHECKS, "single_seat.checks")
        if not all(value is True for value in checks.values()):
            raise ReportError("all single-seat checks must be true")
    elif single["checks"] is not None:
        raise ReportError("non-passed single-seat evidence must use checks=null")

    if fault["status"] == "passed":
        if fault["reference"] != "v1-fault-recovery-evidence.json":
            raise ReportError(
                "passed fault-recovery evidence must reference v1-fault-recovery-evidence.json"
            )
        scenarios = exact_keys(fault["scenarios"], FAULT_SCENARIOS, "fault_recovery.scenarios")
        if not all(value is True for value in scenarios.values()):
            raise ReportError("all fault-recovery scenarios must be true")
    elif fault["scenarios"] is not None:
        raise ReportError("non-passed fault evidence must use scenarios=null")

    if isolation["status"] == "passed":
        for key in ("ordinary_oj_errors", "ordinary_oj_restarts", "ordinary_oj_pid_changes"):
            if require_nonnegative_int(isolation[key], f"ordinary_oj_isolation.{key}") != 0:
                raise ReportError(f"ordinary_oj_isolation.{key} must be zero")

    if capacity["status"] == "passed":
        if capacity["reference"] != "capacity-evidence.json":
            raise ReportError(
                "passed capacity evidence must reference capacity-evidence.json"
            )
        if require_nonnegative_int(capacity["formal_seats"], "capacity.formal_seats") != 15:
            raise ReportError("capacity.formal_seats must equal 15")
        if require_nonnegative_int(capacity["spare_seats"], "capacity.spare_seats") != 2:
            raise ReportError("capacity.spare_seats must equal 2")
        if require_nonnegative_int(capacity["duration_seconds"], "capacity.duration_seconds") < 3600:
            raise ReportError("capacity.duration_seconds must be at least 3600")
        for key in ("unexpected_seat_restarts", "failed_submissions", "failed_collections"):
            if require_nonnegative_int(capacity[key], f"capacity.{key}") != 0:
                raise ReportError(f"capacity.{key} must be zero")
        if require_nonnegative_int(capacity["verified_seats"], "capacity.verified_seats") != 17:
            raise ReportError("capacity.verified_seats must equal 17")
        for key in (
            "spare_takeovers",
            "planned_restart_recoveries",
            "controller_network_recoveries",
        ):
            if require_nonnegative_int(capacity[key], f"capacity.{key}") < 1:
                raise ReportError(f"capacity.{key} must be at least one")
        if capacity["capacity_margin_accepted"] is not True:
            raise ReportError("capacity.capacity_margin_accepted must be true")
    else:
        for key in (
            "formal_seats",
            "spare_seats",
            "duration_seconds",
            "unexpected_seat_restarts",
            "failed_submissions",
            "failed_collections",
            "verified_seats",
            "spare_takeovers",
            "planned_restart_recoveries",
            "controller_network_recoveries",
            "capacity_margin_accepted",
        ):
            if capacity[key] is not None:
                raise ReportError("non-passed capacity evidence must use null summary fields")

    reviewers = report["reviewers"]
    if not isinstance(reviewers, list) or len(reviewers) < 2:
        raise ReportError("at least two reviewers are required")
    normalized_reviewers = [require_nonempty(value, "reviewer") for value in reviewers]
    if len(set(normalized_reviewers)) != len(normalized_reviewers):
        raise ReportError("reviewers must be distinct")

    qualified = report["production_qualified"]
    if not isinstance(qualified, bool):
        raise ReportError("production_qualified must be boolean")
    if qualified != all_passed:
        raise ReportError("production_qualified must exactly match all evidence statuses")
    if require_qualified and not qualified:
        raise ReportError("report is valid but is not production qualified")
    return report


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--require-production-qualified", action="store_true")
    args = parser.parse_args()
    try:
        raw = args.report.read_bytes()
        document = json.loads(raw.decode("utf-8"))
        report = validate_report(
            document, require_qualified=args.require_production_qualified
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ReportError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "qualified" if report["production_qualified"] else "pending",
                "source_revision": report["source_revision"],
                "report_sha256": hashlib.sha256(raw).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
