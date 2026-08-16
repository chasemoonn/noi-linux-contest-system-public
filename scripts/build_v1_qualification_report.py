#!/usr/bin/env python3
"""Compile a fail-closed V1 qualification report from verified evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from verify_v1_capacity_evidence import (
    EvidenceError as CapacityError,
    capacity_summary,
    validate_capacity_evidence,
)
from verify_v1_cross_machine_image_evidence import (
    EvidenceError as CrossMachineError,
    validate_combined as validate_cross_machine,
)
from verify_v1_linux_ci_evidence import (
    EvidenceError as LinuxCIError,
    validate as validate_linux_ci,
    verify_logs,
)
from verify_v1_qualification import ReportError, validate_report
from verify_v1_single_seat_evidence import (
    EvidenceError as SingleSeatError,
    validate_combined as validate_single_seat,
)
from verify_v1_fault_recovery_evidence import (
    EvidenceError as FaultRecoveryError,
    validate_combined as validate_fault_recovery,
)
from verify_v1_independent_teacher_install import (
    EvidenceError as TeacherInstallError,
    validate as validate_teacher_install,
)


class BuildError(RuntimeError):
    pass


def read_json(path: Path, label: str) -> tuple[bytes, dict]:
    if not path.is_file() or path.is_symlink():
        raise BuildError(f"{label} is missing or unsafe")
    raw = path.read_bytes()
    if not 0 < len(raw) <= 32 * 1024 * 1024:
        raise BuildError(f"{label} size is invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise BuildError(f"{label} root differs")
    return raw, value


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def compile_report(
    *, linux_raw: bytes, linux: dict, cross_raw: bytes, cross: dict,
    single_raw: bytes, single: dict, capacity_raw: bytes, capacity: dict,
    fault_raw: bytes | None = None, fault: dict | None = None,
    teacher_raw: bytes | None = None, teacher: dict | None = None,
    reviewers: list[str],
) -> dict:
    revision = capacity["source"]["revision"]
    tree = capacity["source"]["tree"]
    components = capacity["components"]
    if linux["source"] != {"revision": revision, "tree": tree} or \
            cross["source"] != {"revision": revision, "tree": tree} or \
            single["source"] != {"revision": revision, "tree": tree}:
        raise BuildError("qualification evidence source identity differs")
    if single["components"] != components:
        raise BuildError("qualification component identity differs")
    if cross["bundle"]["image_id"] != components["desktop_image_id"]:
        raise BuildError("cross-machine desktop image differs from runtime evidence")
    single_isolation = single["ordinary_oj_isolation"]
    capacity_isolation = capacity["isolation"]
    if any(single_isolation[key] != 0 for key in ("errors", "restarts", "pid_changes")) or \
            any(capacity_isolation[key] != 0 for key in (
                "ordinary_oj_errors", "ordinary_oj_restarts", "ordinary_oj_pid_changes"
            )):
        raise BuildError("ordinary OJ isolation evidence is not clean")
    summary = capacity_summary(capacity)
    fault_row = {
        "status": "pending", "reference": None, "evidence_sha256": None,
        "scenarios": None,
    }
    if fault_raw is not None or fault is not None:
        if fault_raw is None or fault is None:
            raise BuildError("fault-recovery evidence bytes and document must be supplied together")
        if fault["source"] != {"revision": revision, "tree": tree} or \
                fault["components"] != components or \
                fault["session_id"] != capacity["session_id"]:
            raise BuildError("fault-recovery evidence identity differs")
        fault_row = {
            "status": "passed", "reference": "v1-fault-recovery-evidence.json",
            "evidence_sha256": sha256(fault_raw), "scenarios": fault["scenarios"],
        }
    teacher_row = {"status": "pending", "reference": None, "evidence_sha256": None}
    if teacher_raw is not None or teacher is not None:
        if teacher_raw is None or teacher is None:
            raise BuildError("teacher-install evidence bytes and document must be supplied together")
        if teacher["source"] != {"revision": revision, "tree": tree} or teacher["components"] != components:
            raise BuildError("teacher-install evidence identity differs")
        teacher_row = {"status": "passed", "reference": "v1-independent-teacher-install-evidence.json",
                       "evidence_sha256": sha256(teacher_raw)}
    report = {
        "$schema": "v1-qualification-report.schema.json",
        "schema_version": 2,
        "source_revision": revision,
        "profile": capacity["environment"]["profile"],
        "components": components,
        "evidence": {
            "linux_ci": {"status": "passed", "reference": "v1-linux-ci-evidence.json",
                         "evidence_sha256": sha256(linux_raw)},
            "cross_machine_import_rollback": {
                "status": "passed", "reference": "v1-cross-machine-image-evidence.json",
                "evidence_sha256": sha256(cross_raw),
            },
            "independent_teacher_install": teacher_row,
            "single_seat": {
                "status": "passed", "reference": "single-seat-evidence.json",
                "evidence_sha256": sha256(single_raw), "checks": single["checks"],
            },
            "fault_recovery": fault_row,
            "ordinary_oj_isolation": {
                "status": "passed", "reference": "capacity-evidence.json",
                "evidence_sha256": sha256(capacity_raw), "ordinary_oj_errors": 0,
                "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0,
            },
            "capacity_15_plus_2": {
                "status": "passed", "reference": "capacity-evidence.json",
                "evidence_sha256": sha256(capacity_raw), **summary,
            },
        },
        "reviewers": reviewers,
        "production_qualified": teacher is not None and fault is not None,
    }
    return validate_report(report)


def atomic_write(path: Path, document: dict) -> str:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        raise BuildError("qualification report output already exists")
    parent = requested.parent.resolve(strict=True)
    raw = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    descriptor, name = tempfile.mkstemp(prefix=".qualification-report-", dir=parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, requested)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return sha256(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--linux-ci", required=True, type=Path)
    parser.add_argument("--linux-ci-log-directory", required=True, type=Path)
    parser.add_argument("--cross-machine", required=True, type=Path)
    parser.add_argument("--single-seat", required=True, type=Path)
    parser.add_argument("--capacity", required=True, type=Path)
    parser.add_argument("--capacity-artifact-root", required=True, type=Path)
    parser.add_argument("--fault-recovery", type=Path)
    parser.add_argument("--independent-teacher-install", type=Path)
    parser.add_argument("--reviewer", action="append", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if len(args.reviewer) < 2 or len(set(args.reviewer)) != len(args.reviewer):
            raise BuildError("at least two distinct reviewers are required")
        linux_raw, linux_value = read_json(args.linux_ci, "Linux CI evidence")
        linux = validate_linux_ci(linux_value)
        verify_logs(linux, args.linux_ci_log_directory)
        cross_raw, cross_value = read_json(args.cross_machine, "cross-machine evidence")
        cross = validate_cross_machine(cross_value)
        single_raw, single_value = read_json(args.single_seat, "single-seat evidence")
        single = validate_single_seat(single_value)
        capacity_raw, capacity_value = read_json(args.capacity, "capacity evidence")
        capacity = validate_capacity_evidence(
            capacity_value, artifact_root=args.capacity_artifact_root
        )
        fault_raw = fault = None
        if args.fault_recovery is not None:
            fault_raw, fault_value = read_json(args.fault_recovery, "fault-recovery evidence")
            fault = validate_fault_recovery(
                fault_value,
                expected_revision=capacity["source"]["revision"],
                expected_components=capacity["components"],
                capacity=capacity,
                capacity_raw=capacity_raw,
            )
        teacher_raw = teacher = None
        if args.independent_teacher_install is not None:
            teacher_raw, teacher_value = read_json(args.independent_teacher_install, "teacher-install evidence")
            teacher = validate_teacher_install(
                teacher_value,
                expected_revision=capacity["source"]["revision"],
                expected_tree=capacity["source"]["tree"],
                expected_components=capacity["components"],
                ssh_keygen=Path("/usr/bin/ssh-keygen"),
            )
        report = compile_report(
            linux_raw=linux_raw, linux=linux, cross_raw=cross_raw, cross=cross,
            single_raw=single_raw, single=single, capacity_raw=capacity_raw,
            capacity=capacity, reviewers=args.reviewer,
            fault_raw=fault_raw, fault=fault,
            teacher_raw=teacher_raw, teacher=teacher,
        )
        digest = atomic_write(args.output, report)
        remaining = []
        if teacher is None:
            remaining.append("independent_teacher_install")
        if fault is None:
            remaining.append("fault_recovery")
        print(json.dumps({"remaining": remaining, "report_sha256": digest,
                          "status": "passed" if not remaining else "pending"}, sort_keys=True))
        return 0
    except (BuildError, LinuxCIError, CrossMachineError, SingleSeatError,
            CapacityError, FaultRecoveryError, TeacherInstallError, ReportError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
