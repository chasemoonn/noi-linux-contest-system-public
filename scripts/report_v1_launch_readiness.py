#!/usr/bin/env python3
"""Report the exact remaining NOI Linux V1 launch gates without mutating anything."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from verify_v1_qualification import ReportError, validate_report


GATES = (
    ("linux_ci", "Linux 全量 CI"),
    ("cross_machine_import_rollback", "跨机镜像导入与回滚"),
    ("single_seat", "单座端到端演练"),
    ("fault_recovery", "六类故障恢复"),
    ("ordinary_oj_isolation", "普通 OJ 隔离"),
    ("capacity_15_plus_2", "15+2 一小时容量"),
    ("independent_teacher_install", "独立老师安装与回滚"),
)

DELIVERY_GATES = (
    ("source_release_transaction", "passed"),
    ("exact_backup_manifest", "passed"),
    ("exact_rollback_verifier", "passed"),
    ("service_apply_coordinator", "passed"),
)


def build(report: dict) -> dict:
    checked = validate_report(report)
    gates = []
    for code, title in GATES:
        row = checked["evidence"][code]
        gates.append({
            "code": code,
            "title": title,
            "status": row["status"],
            "evidence_sha256": row.get("evidence_sha256"),
        })
    passed = sum(row["status"] == "passed" for row in gates)
    failed = sum(row["status"] == "failed" for row in gates)
    pending = len(gates) - passed - failed
    delivery = [{"code": code, "status": status} for code, status in DELIVERY_GATES]
    # The independently signed teacher-install evidence is built only after
    # validating the clean-install matrix: one success plus phase-failure and
    # power-loss rollback at all six phases.  It is therefore the report-level
    # proof that the final software-delivery gate has actually run.  Keeping
    # this row permanently pending made the apply readiness mathematically
    # unreachable even for a fully qualified report.
    clean_matrix_status = (
        "passed"
        if checked["evidence"]["independent_teacher_install"]["status"]
        == "passed"
        else "pending"
    )
    delivery.append({
        "code": "linux_clean_install_rehearsal_matrix",
        "status": clean_matrix_status,
    })
    delivery_complete = all(row["status"] == "passed" for row in delivery)
    return {
        "schema_version": 1,
        "profile": checked["profile"],
        "source_revision": checked["source_revision"],
        "production_qualified": checked["production_qualified"],
        "summary": {"total": len(gates), "passed": passed, "pending": pending, "failed": failed},
        "gates": gates,
        "next_actions": [row["code"] for row in gates if row["status"] != "passed"],
        "software_delivery": {
            "gates": delivery,
            "complete": delivery_complete,
            "next_actions": [row["code"] for row in delivery if row["status"] != "passed"],
        },
        "production_install_plan_available": checked["production_qualified"],
        "production_install_apply_available": checked["production_qualified"] and delivery_complete,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        value = json.loads(args.report.read_text(encoding="utf-8"))
        print(json.dumps(build(value), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ReportError) as exc:
        print(f"NO_GO: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
