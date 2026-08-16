#!/usr/bin/env python3
"""Read-only verification for an NOI Linux V1 source candidate directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from build_v1_candidate import CandidateError, validate_source_path, verify_archive
from verify_v1_qualification import ReportError, validate_report
from verify_v1_single_seat_evidence import (
    EvidenceError as SingleSeatEvidenceError,
    validate_combined as validate_single_seat_evidence,
)
from verify_v1_capacity_evidence import (
    EvidenceError as CapacityEvidenceError,
    capacity_summary,
    validate_capacity_evidence,
)
from verify_v1_linux_ci_evidence import (
    EvidenceError as LinuxCIEvidenceError,
    validate as validate_linux_ci_evidence,
    verify_logs as verify_linux_ci_logs,
)
from verify_v1_cross_machine_image_evidence import (
    EvidenceError as CrossMachineEvidenceError,
    validate_combined as validate_cross_machine_evidence,
)
from verify_v1_fault_recovery_evidence import (
    EvidenceError as FaultRecoveryEvidenceError,
    validate_combined as validate_fault_recovery_evidence,
)
from verify_v1_independent_teacher_install import (
    EvidenceError as TeacherInstallEvidenceError,
    validate as validate_teacher_install_evidence,
)


HEX40 = re.compile(r"^[a-f0-9]{40}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CandidateError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--require-production-qualified", action="store_true")
    args = parser.parse_args()
    try:
        candidate = args.candidate.resolve()
        require(candidate.is_dir() and not candidate.is_symlink(), "candidate must be a real directory")
        names = {path.name for path in candidate.iterdir()}
        require("candidate-manifest.json" in names, "candidate manifest is missing")
        manifest_path = candidate / "candidate-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        require(
            set(manifest)
            == {"$schema", "schema_version", "candidate", "source", "static_gates", "qualification"},
            "candidate manifest shape differs",
        )
        require(manifest.get("$schema") == "v1-source-candidate.schema.json", "unexpected candidate schema")
        require(manifest.get("schema_version") == 1, "unsupported candidate schema")
        candidate_row = manifest.get("candidate")
        require(
            isinstance(candidate_row, dict)
            and set(candidate_row) == {"version", "profile", "product_contract"},
            "candidate identity is invalid",
        )
        require(candidate_row["profile"] == "aliyun-hydro5-pm2-direct-v1", "candidate profile differs")
        require(candidate_row["product_contract"] == "NOI Linux V1", "product contract differs")
        require(
            bool(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", str(candidate_row["version"]))),
            "candidate version is invalid",
        )
        gates = manifest.get("static_gates")
        require(
            gates
            == {
                "submission_fault_injection": "passed",
                "v1_product_contract": "passed",
                "public_release_boundary": "passed",
            },
            "static gates are not passed",
        )
        source = manifest.get("source")
        require(isinstance(source, dict), "source manifest is missing")
        require(
            set(source) == {"revision", "tree", "archive", "tracked_file_count", "files"},
            "source manifest shape differs",
        )
        require(bool(HEX40.fullmatch(str(source.get("revision", "")))), "source revision is invalid")
        require(bool(HEX40.fullmatch(str(source.get("tree", "")))), "source tree is invalid")
        files = source.get("files")
        require(isinstance(files, list) and files, "source file manifest is empty")
        require(source.get("tracked_file_count") == len(files), "tracked file count differs")
        paths = [row.get("path") for row in files if isinstance(row, dict)]
        require(len(paths) == len(files) and paths == sorted(paths), "source paths are not sorted")
        require(len(set(paths)) == len(paths), "source paths contain duplicates")
        for row in files:
            require(set(row) == {"path", "mode", "bytes", "sha256"}, "source file row shape differs")
            require(row["mode"] in {"100644", "100755"}, "source file mode is invalid")
            require(isinstance(row["bytes"], int) and row["bytes"] >= 0, "source file size is invalid")
            require(bool(HEX64.fullmatch(str(row["sha256"]))), "source file digest is invalid")
            validate_source_path(str(row["path"]))
        archive_row = source.get("archive")
        require(isinstance(archive_row, dict), "archive metadata is missing")
        require(set(archive_row) == {"name", "sha256", "bytes", "root"}, "archive metadata shape differs")
        require(archive_row["root"] == "noi-linux-contest-system-v1/", "archive root differs")
        archive_name = archive_row.get("name")
        require(isinstance(archive_name, str) and Path(archive_name).name == archive_name, "archive name is unsafe")
        archive = candidate / archive_name
        require(archive.is_file() and not archive.is_symlink(), "archive is missing or unsafe")
        require(archive.stat().st_size == archive_row.get("bytes"), "archive size differs")
        require(sha256_file(archive) == archive_row.get("sha256"), "archive SHA256 differs")
        verify_archive(archive, files)

        qualification = manifest.get("qualification")
        require(isinstance(qualification, dict), "qualification metadata is missing")
        require(
            set(qualification) == {"production_qualified", "report", "report_sha256"},
            "qualification metadata shape differs",
        )
        qualified = qualification.get("production_qualified")
        require(isinstance(qualified, bool), "production qualification flag is invalid")
        report_name = qualification.get("report")
        report_sha = qualification.get("report_sha256")
        if report_name is None:
            require(report_sha is None and not qualified, "missing report cannot be production qualified")
        else:
            require(isinstance(report_name, str) and Path(report_name).name == report_name, "qualification report name is unsafe")
            report_path = candidate / report_name
            require(report_path.is_file() and not report_path.is_symlink(), "qualification report is missing")
            report_bytes = report_path.read_bytes()
            require(hashlib.sha256(report_bytes).hexdigest() == report_sha, "qualification report SHA256 differs")
            report = validate_report(json.loads(report_bytes.decode("utf-8")))
            require(report["source_revision"] == source["revision"], "qualification revision differs")
            require(report["production_qualified"] == qualified, "qualification flag differs from report")
            linux_ci = report["evidence"]["linux_ci"]
            if linux_ci["status"] == "passed":
                require(linux_ci["reference"] == "v1-linux-ci-evidence.json",
                        "Linux CI reference is unexpected")
                linux_ci_path = candidate / linux_ci["reference"]
                require(linux_ci_path.is_file() and not linux_ci_path.is_symlink(),
                        "Linux CI evidence is missing")
                linux_ci_bytes = linux_ci_path.read_bytes()
                require(hashlib.sha256(linux_ci_bytes).hexdigest() == linux_ci["evidence_sha256"],
                        "Linux CI evidence SHA256 differs")
                linux_ci_document = validate_linux_ci_evidence(
                    json.loads(linux_ci_bytes.decode("utf-8")), source["revision"]
                )
                require(linux_ci_document["source"]["tree"] == source["tree"],
                        "Linux CI source tree differs")
                verify_linux_ci_logs(linux_ci_document, candidate / "v1-linux-ci-logs")
            cross_machine = report["evidence"]["cross_machine_import_rollback"]
            if cross_machine["status"] == "passed":
                require(cross_machine["reference"] == "v1-cross-machine-image-evidence.json",
                        "cross-machine reference is unexpected")
                cross_path = candidate / cross_machine["reference"]
                require(cross_path.is_file() and not cross_path.is_symlink(),
                        "cross-machine evidence is missing")
                cross_bytes = cross_path.read_bytes()
                require(hashlib.sha256(cross_bytes).hexdigest() == cross_machine["evidence_sha256"],
                        "cross-machine evidence SHA256 differs")
                validate_cross_machine_evidence(
                    json.loads(cross_bytes.decode("utf-8")),
                    expected_revision=source["revision"], expected_tree=source["tree"],
                    expected_image_id=report["components"]["desktop_image_id"],
                )
            teacher_install = report["evidence"]["independent_teacher_install"]
            if teacher_install["status"] == "passed":
                require(teacher_install["reference"] == "v1-independent-teacher-install-evidence.json",
                        "teacher-install reference is unexpected")
                teacher_path = candidate / teacher_install["reference"]
                require(teacher_path.is_file() and not teacher_path.is_symlink(),
                        "teacher-install evidence is missing")
                teacher_bytes = teacher_path.read_bytes()
                require(hashlib.sha256(teacher_bytes).hexdigest() == teacher_install["evidence_sha256"],
                        "teacher-install evidence SHA256 differs")
                validate_teacher_install_evidence(
                    json.loads(teacher_bytes.decode("utf-8")),
                    expected_revision=source["revision"], expected_tree=source["tree"],
                    expected_components=report["components"],
                    expected_archive_sha256=archive_row["sha256"],
                    ssh_keygen=Path(shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"),
                )
            single_seat = report["evidence"]["single_seat"]
            if single_seat["status"] == "passed":
                single_seat_name = single_seat["reference"]
                require(
                    single_seat_name == "single-seat-evidence.json",
                    "single-seat evidence reference is unsafe or unexpected",
                )
                single_seat_path = candidate / single_seat_name
                require(
                    single_seat_path.is_file() and not single_seat_path.is_symlink(),
                    "single-seat evidence is missing",
                )
                single_seat_bytes = single_seat_path.read_bytes()
                require(
                    hashlib.sha256(single_seat_bytes).hexdigest()
                    == single_seat["evidence_sha256"],
                    "single-seat evidence SHA256 differs",
                )
                single_seat_document = json.loads(single_seat_bytes.decode("utf-8"))
                validate_single_seat_evidence(
                    single_seat_document,
                    expected_revision=source["revision"],
                    expected_components=report["components"],
                )
                require(
                    single_seat_document["checks"] == single_seat["checks"],
                    "single-seat checks differ from the qualification report",
                )
            capacity = report["evidence"]["capacity_15_plus_2"]
            capacity_document = None
            if capacity["status"] == "passed":
                capacity_name = capacity["reference"]
                require(
                    capacity_name == "capacity-evidence.json",
                    "capacity evidence reference is unsafe or unexpected",
                )
                capacity_path = candidate / capacity_name
                require(
                    capacity_path.is_file() and not capacity_path.is_symlink(),
                    "capacity evidence is missing",
                )
                capacity_bytes = capacity_path.read_bytes()
                require(
                    hashlib.sha256(capacity_bytes).hexdigest()
                    == capacity["evidence_sha256"],
                    "capacity evidence SHA256 differs",
                )
                capacity_document = json.loads(capacity_bytes.decode("utf-8"))
                validate_capacity_evidence(
                    capacity_document,
                    expected_revision=source["revision"],
                    expected_tree=source["tree"],
                    expected_components=report["components"],
                )
                require(
                    capacity_summary(capacity_document)
                    == {
                        key: capacity[key]
                        for key in capacity_summary(capacity_document)
                    },
                    "capacity summary differs from the qualification report",
                )
            fault_recovery = report["evidence"]["fault_recovery"]
            if fault_recovery["status"] == "passed":
                fault_name = fault_recovery["reference"]
                require(
                    fault_name == "v1-fault-recovery-evidence.json",
                    "fault-recovery reference is unsafe or unexpected",
                )
                fault_path = candidate / fault_name
                require(
                    fault_path.is_file() and not fault_path.is_symlink(),
                    "fault-recovery evidence is missing",
                )
                fault_bytes = fault_path.read_bytes()
                require(
                    hashlib.sha256(fault_bytes).hexdigest()
                    == fault_recovery["evidence_sha256"],
                    "fault-recovery evidence SHA256 differs",
                )
                fault_document = json.loads(fault_bytes.decode("utf-8"))
                validate_fault_recovery_evidence(
                    fault_document,
                    expected_revision=source["revision"],
                    expected_components=report["components"],
                    capacity=capacity_document,
                    capacity_raw=capacity_bytes,
                )
                require(
                    fault_document["scenarios"] == fault_recovery["scenarios"],
                    "fault-recovery scenarios differ from the qualification report",
                )
                require(
                    capacity_document is not None
                    and fault_document["session_id"] == capacity_document["session_id"],
                    "fault-recovery capacity session differs",
                )
        allowed = {"candidate-manifest.json", archive_name}
        if report_name is not None:
            allowed.add(report_name)
            if report["evidence"]["linux_ci"]["status"] == "passed":
                allowed.update({"v1-linux-ci-evidence.json", "v1-linux-ci-logs"})
            if report["evidence"]["cross_machine_import_rollback"]["status"] == "passed":
                allowed.add("v1-cross-machine-image-evidence.json")
            if report["evidence"]["single_seat"]["status"] == "passed":
                allowed.add("single-seat-evidence.json")
            if report["evidence"]["capacity_15_plus_2"]["status"] == "passed":
                allowed.add("capacity-evidence.json")
            if report["evidence"]["fault_recovery"]["status"] == "passed":
                allowed.add("v1-fault-recovery-evidence.json")
            if report["evidence"]["independent_teacher_install"]["status"] == "passed":
                allowed.add("v1-independent-teacher-install-evidence.json")
        require(names == allowed, f"candidate directory has unexpected entries: {sorted(names - allowed)}")
        if args.require_production_qualified:
            require(qualified is True, "candidate is valid but not production qualified")
        print(json.dumps({"status": "qualified" if qualified else "candidate", "revision": source["revision"], "archive_sha256": archive_row["sha256"], "manifest_sha256": sha256_file(manifest_path)}, sort_keys=True))
        return 0
    except (CandidateError, ReportError, SingleSeatEvidenceError, CapacityEvidenceError,
            LinuxCIEvidenceError, CrossMachineEvidenceError, FaultRecoveryEvidenceError,
            TeacherInstallEvidenceError,
            OSError, UnicodeDecodeError, subprocess.SubprocessError,
            json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
