#!/usr/bin/env python3
"""Create and self-verify a source-only NOI Linux V1 candidate bundle."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

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


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "aliyun-hydro5-pm2-direct-v1"
ARCHIVE_ROOT = "noi-linux-contest-system-v1/"
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")


class CandidateError(ValueError):
    pass


def git(*arguments: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_clean_worktree() -> None:
    if git("status", "--porcelain=v1", "--untracked-files=all").strip():
        raise CandidateError("Git worktree is not clean")


def validate_source_path(path: str) -> None:
    pure = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or path.startswith("/")
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise CandidateError(f"unsafe tracked path: {path!r}")


def run_static_gate(script_name: str) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CandidateError(f"{script_name} failed: {detail[:500]}")


def run_fault_injection_gate() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_v1_fault_injection.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CandidateError(f"fault injection gate failed: {detail[-1000:]}")


def tracked_files(revision: str) -> tuple[list[dict[str, object]], dict[str, str]]:
    raw = git("ls-tree", "-r", "-z", "--full-tree", revision, text=False)
    rows: list[dict[str, object]] = []
    object_ids: dict[str, str] = {}
    assert isinstance(raw, bytes)
    for item in raw.split(b"\0"):
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split(" ")
        path = raw_path.decode("utf-8")
        validate_source_path(path)
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise CandidateError(f"unsupported tracked entry: {mode} {kind} {path}")
        content = git("cat-file", "blob", object_id, text=False)
        assert isinstance(content, bytes)
        rows.append(
            {
                "path": path,
                "mode": mode,
                "bytes": len(content),
                "sha256": sha256_bytes(content),
            }
        )
        object_ids[path] = object_id
    rows.sort(key=lambda row: str(row["path"]))
    return rows, object_ids


def create_source_archive(
    archive: Path,
    revision: str,
    files: list[dict[str, object]],
    object_ids: dict[str, str],
) -> None:
    """Write a platform-independent tar directly from committed Git blobs."""
    timestamp = int(str(git("show", "-s", "--format=%ct", revision)).strip())
    directories = {ARCHIVE_ROOT.rstrip("/")}
    for row in files:
        parent = PurePosixPath(f"{ARCHIVE_ROOT}{row['path']}").parent
        while str(parent) not in {"", "."}:
            directories.add(str(parent))
            parent = parent.parent

    def normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        info.mtime = timestamp
        return info

    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as bundle:
        for name in sorted(directories, key=lambda value: (value.count("/"), value)):
            info = normalized(tarfile.TarInfo(f"{name}/"))
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.size = 0
            bundle.addfile(info)
        for row in files:
            path = str(row["path"])
            content = git("cat-file", "blob", object_ids[path], text=False)
            assert isinstance(content, bytes)
            info = normalized(tarfile.TarInfo(f"{ARCHIVE_ROOT}{path}"))
            info.type = tarfile.REGTYPE
            info.mode = 0o755 if row["mode"] == "100755" else 0o644
            info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))


def verify_archive(archive: Path, files: list[dict[str, object]]) -> None:
    expected = {f"{ARCHIVE_ROOT}{row['path']}": row for row in files}
    expected_directories = {ARCHIVE_ROOT.rstrip("/")}
    for name in expected:
        parent = PurePosixPath(name).parent
        while str(parent) not in {"", "."}:
            expected_directories.add(str(parent))
            parent = parent.parent
    observed: set[str] = set()
    with tarfile.open(archive, "r:") as bundle:
        for member in bundle.getmembers():
            if member.isdir():
                if member.name.rstrip("/") not in expected_directories:
                    raise CandidateError(
                        f"archive contains an unexpected directory: {member.name}"
                    )
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise CandidateError(f"archive contains a non-regular entry: {member.name}")
            if member.name not in expected:
                raise CandidateError(f"archive contains an unexpected file: {member.name}")
            if member.name in observed:
                raise CandidateError(f"archive contains a duplicate file: {member.name}")
            handle = bundle.extractfile(member)
            if handle is None:
                raise CandidateError(f"archive member cannot be read: {member.name}")
            content = handle.read()
            row = expected[member.name]
            if len(content) != row["bytes"] or sha256_bytes(content) != row["sha256"]:
                raise CandidateError(f"archive member digest differs: {member.name}")
            expected_mode = int(str(row["mode"])[-3:], 8)
            if member.mode & 0o777 != expected_mode:
                raise CandidateError(f"archive member mode differs: {member.name}")
            observed.add(member.name)
    missing = sorted(set(expected) - observed)
    if missing:
        raise CandidateError(f"archive is missing tracked files: {missing[:3]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-root", type=Path, default=ROOT / "local-release")
    parser.add_argument("--qualification-report", type=Path)
    parser.add_argument("--linux-ci-evidence", type=Path)
    parser.add_argument("--linux-ci-log-directory", type=Path)
    parser.add_argument("--cross-machine-evidence", type=Path)
    parser.add_argument("--single-seat-evidence", type=Path)
    parser.add_argument("--capacity-evidence", type=Path)
    parser.add_argument("--capacity-artifact-root", type=Path)
    parser.add_argument("--fault-recovery-evidence", type=Path)
    parser.add_argument("--independent-teacher-install-evidence", type=Path)
    args = parser.parse_args()
    try:
        if not VERSION.fullmatch(args.version):
            raise CandidateError("--version must be SemVer")
        require_clean_worktree()
        run_static_gate("check_v1_product_contract.py")
        run_static_gate("check_public_release.py")
        run_fault_injection_gate()
        revision = str(git("rev-parse", "HEAD")).strip()
        tree = str(git("rev-parse", "HEAD^{tree}")).strip()
        files, object_ids = tracked_files(revision)

        qualification = {
            "production_qualified": False,
            "report": None,
            "report_sha256": None,
        }
        report_bytes: bytes | None = None
        report_name: str | None = None
        single_seat_bytes: bytes | None = None
        single_seat_name: str | None = None
        capacity_bytes: bytes | None = None
        capacity_name: str | None = None
        fault_recovery_bytes: bytes | None = None
        fault_recovery_name: str | None = None
        teacher_install_bytes: bytes | None = None
        teacher_install_name: str | None = None
        teacher_install_document: dict | None = None
        linux_ci_bytes: bytes | None = None
        linux_ci_name: str | None = None
        cross_machine_bytes: bytes | None = None
        cross_machine_name: str | None = None
        if args.qualification_report:
            report_bytes = args.qualification_report.read_bytes()
            report = validate_report(json.loads(report_bytes.decode("utf-8")))
            if report["source_revision"] != revision:
                raise CandidateError("qualification report revision differs from HEAD")
            report_name = "qualification-report.json"
            qualification = {
                "production_qualified": bool(report["production_qualified"]),
                "report": report_name,
                "report_sha256": sha256_bytes(report_bytes),
            }
            linux_ci = report["evidence"]["linux_ci"]
            if linux_ci["status"] == "passed":
                if args.linux_ci_evidence is None or args.linux_ci_log_directory is None:
                    raise CandidateError("passed Linux CI requires evidence and complete logs")
                linux_ci_name = str(linux_ci["reference"])
                if linux_ci_name != "v1-linux-ci-evidence.json":
                    raise CandidateError("passed Linux CI reference differs")
                linux_ci_bytes = args.linux_ci_evidence.read_bytes()
                if sha256_bytes(linux_ci_bytes) != linux_ci["evidence_sha256"]:
                    raise CandidateError("Linux CI evidence SHA256 differs from the report")
                linux_ci_document = validate_linux_ci_evidence(
                    json.loads(linux_ci_bytes.decode("utf-8")), revision
                )
                if linux_ci_document["source"]["tree"] != tree:
                    raise CandidateError("Linux CI source tree differs from HEAD")
                verify_linux_ci_logs(linux_ci_document, args.linux_ci_log_directory)
            elif args.linux_ci_evidence is not None or args.linux_ci_log_directory is not None:
                raise CandidateError("Linux CI inputs require passed report evidence")
            cross_machine = report["evidence"]["cross_machine_import_rollback"]
            if cross_machine["status"] == "passed":
                if args.cross_machine_evidence is None:
                    raise CandidateError("passed cross-machine evidence must be supplied")
                cross_machine_name = str(cross_machine["reference"])
                if cross_machine_name != "v1-cross-machine-image-evidence.json":
                    raise CandidateError("passed cross-machine reference differs")
                cross_machine_bytes = args.cross_machine_evidence.read_bytes()
                if sha256_bytes(cross_machine_bytes) != cross_machine["evidence_sha256"]:
                    raise CandidateError("cross-machine evidence SHA256 differs from the report")
                validate_cross_machine_evidence(
                    json.loads(cross_machine_bytes.decode("utf-8")),
                    expected_revision=revision, expected_tree=tree,
                    expected_image_id=report["components"]["desktop_image_id"],
                )
            elif args.cross_machine_evidence is not None:
                raise CandidateError("cross-machine input requires passed report evidence")
            teacher_install = report["evidence"]["independent_teacher_install"]
            if teacher_install["status"] == "passed":
                if args.independent_teacher_install_evidence is None:
                    raise CandidateError("passed teacher-install evidence must be supplied")
                teacher_install_name = str(teacher_install["reference"])
                if teacher_install_name != "v1-independent-teacher-install-evidence.json":
                    raise CandidateError("teacher-install reference differs")
                teacher_install_bytes = args.independent_teacher_install_evidence.read_bytes()
                if sha256_bytes(teacher_install_bytes) != teacher_install["evidence_sha256"]:
                    raise CandidateError("teacher-install evidence SHA256 differs from report")
                teacher_install_document = validate_teacher_install_evidence(
                    json.loads(teacher_install_bytes.decode("utf-8")),
                    expected_revision=revision, expected_tree=tree,
                    expected_components=report["components"],
                    ssh_keygen=Path(shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"),
                )
            elif args.independent_teacher_install_evidence is not None:
                raise CandidateError("teacher-install input requires passed report evidence")
            single_seat = report["evidence"]["single_seat"]
            if single_seat["status"] == "passed":
                if args.single_seat_evidence is None:
                    raise CandidateError(
                        "passed single-seat evidence must be supplied to the candidate builder"
                    )
                reference = str(single_seat["reference"])
                if Path(reference).name != reference or reference != "single-seat-evidence.json":
                    raise CandidateError(
                        "passed single-seat reference must equal single-seat-evidence.json"
                    )
                single_seat_bytes = args.single_seat_evidence.read_bytes()
                if sha256_bytes(single_seat_bytes) != single_seat["evidence_sha256"]:
                    raise CandidateError("single-seat evidence SHA256 differs from the report")
                single_seat_document = json.loads(single_seat_bytes.decode("utf-8"))
                validate_single_seat_evidence(
                    single_seat_document,
                    expected_revision=revision,
                    expected_components=report["components"],
                )
                if single_seat_document["checks"] != single_seat["checks"]:
                    raise CandidateError("single-seat checks differ from the report")
                single_seat_name = reference
            elif args.single_seat_evidence is not None:
                raise CandidateError(
                    "single-seat evidence cannot be supplied unless the report marks it passed"
                )
            capacity = report["evidence"]["capacity_15_plus_2"]
            if capacity["status"] == "passed":
                if args.capacity_evidence is None:
                    raise CandidateError(
                        "passed capacity evidence must be supplied to the candidate builder"
                    )
                if args.capacity_artifact_root is None:
                    raise CandidateError(
                        "passed capacity evidence requires --capacity-artifact-root"
                    )
                reference = str(capacity["reference"])
                if Path(reference).name != reference or reference != "capacity-evidence.json":
                    raise CandidateError(
                        "passed capacity reference must equal capacity-evidence.json"
                    )
                capacity_bytes = args.capacity_evidence.read_bytes()
                if sha256_bytes(capacity_bytes) != capacity["evidence_sha256"]:
                    raise CandidateError("capacity evidence SHA256 differs from the report")
                capacity_document = json.loads(capacity_bytes.decode("utf-8"))
                validate_capacity_evidence(
                    capacity_document,
                    expected_revision=revision,
                    expected_tree=tree,
                    expected_components=report["components"],
                    artifact_root=args.capacity_artifact_root,
                )
                if capacity_summary(capacity_document) != {
                    key: capacity[key]
                    for key in capacity_summary(capacity_document)
                }:
                    raise CandidateError("capacity summary differs from the report")
                capacity_name = reference
            elif args.capacity_evidence is not None or args.capacity_artifact_root is not None:
                raise CandidateError(
                    "capacity evidence inputs cannot be supplied unless the report marks it passed"
                )
            fault_recovery = report["evidence"]["fault_recovery"]
            if fault_recovery["status"] == "passed":
                if args.fault_recovery_evidence is None:
                    raise CandidateError(
                        "passed fault-recovery evidence must be supplied to the candidate builder"
                    )
                fault_recovery_name = str(fault_recovery["reference"])
                if fault_recovery_name != "v1-fault-recovery-evidence.json":
                    raise CandidateError("passed fault-recovery reference differs")
                fault_recovery_bytes = args.fault_recovery_evidence.read_bytes()
                if sha256_bytes(fault_recovery_bytes) != fault_recovery["evidence_sha256"]:
                    raise CandidateError("fault-recovery evidence SHA256 differs from the report")
                fault_document = json.loads(fault_recovery_bytes.decode("utf-8"))
                validate_fault_recovery_evidence(
                    fault_document,
                    expected_revision=revision,
                    expected_components=report["components"],
                    capacity=capacity_document,
                    capacity_raw=capacity_bytes,
                )
                if fault_document["scenarios"] != fault_recovery["scenarios"]:
                    raise CandidateError("fault-recovery scenarios differ from the report")
                if capacity_bytes is None:
                    raise CandidateError("passed fault recovery requires capacity evidence")
                if fault_document["session_id"] != capacity_document["session_id"]:
                    raise CandidateError("fault-recovery capacity session differs")
            elif args.fault_recovery_evidence is not None:
                raise CandidateError(
                    "fault-recovery evidence cannot be supplied unless the report marks it passed"
                )
        elif (
            args.linux_ci_evidence is not None
            or args.linux_ci_log_directory is not None
            or args.cross_machine_evidence is not None
            or
            args.single_seat_evidence is not None
            or args.capacity_evidence is not None
            or args.capacity_artifact_root is not None
            or args.fault_recovery_evidence is not None
            or args.independent_teacher_install_evidence is not None
        ):
            raise CandidateError("raw qualification evidence requires a qualification report")

        output_root = args.output_root.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        final = output_root / f"noi-v1-{args.version}-{revision[:12]}"
        if final.exists():
            raise CandidateError(f"candidate directory already exists: {final}")
        with tempfile.TemporaryDirectory(prefix="noi-v1-build-", dir=output_root) as raw_stage:
            stage = Path(raw_stage)
            archive_name = f"noi-linux-contest-system-v1-{args.version}.tar"
            archive = stage / archive_name
            create_source_archive(archive, revision, files, object_ids)
            verify_archive(archive, files)
            if teacher_install_document is not None:
                validate_teacher_install_evidence(
                    teacher_install_document,
                    expected_revision=revision,
                    expected_tree=tree,
                    expected_components=report["components"],
                    expected_archive_sha256=sha256_file(archive),
                    ssh_keygen=Path(shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"),
                )
            manifest = {
                "$schema": "v1-source-candidate.schema.json",
                "schema_version": 1,
                "candidate": {
                    "version": args.version,
                    "profile": PROFILE,
                    "product_contract": "NOI Linux V1",
                },
                "source": {
                    "revision": revision,
                    "tree": tree,
                    "archive": {
                        "name": archive_name,
                        "sha256": sha256_file(archive),
                        "bytes": archive.stat().st_size,
                        "root": ARCHIVE_ROOT,
                    },
                    "tracked_file_count": len(files),
                    "files": files,
                },
                "static_gates": {
                    "submission_fault_injection": "passed",
                    "v1_product_contract": "passed",
                    "public_release_boundary": "passed",
                },
                "qualification": qualification,
            }
            (stage / "candidate-manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            if report_bytes is not None and report_name is not None:
                (stage / report_name).write_bytes(report_bytes)
            if single_seat_bytes is not None and single_seat_name is not None:
                (stage / single_seat_name).write_bytes(single_seat_bytes)
            if capacity_bytes is not None and capacity_name is not None:
                (stage / capacity_name).write_bytes(capacity_bytes)
            if linux_ci_bytes is not None and linux_ci_name is not None:
                (stage / linux_ci_name).write_bytes(linux_ci_bytes)
            if cross_machine_bytes is not None and cross_machine_name is not None:
                (stage / cross_machine_name).write_bytes(cross_machine_bytes)
            if fault_recovery_bytes is not None and fault_recovery_name is not None:
                (stage / fault_recovery_name).write_bytes(fault_recovery_bytes)
            if teacher_install_bytes is not None and teacher_install_name is not None:
                (stage / teacher_install_name).write_bytes(teacher_install_bytes)
            if linux_ci_bytes is not None:
                target_logs = stage / "v1-linux-ci-logs"
                target_logs.mkdir()
                for path in sorted(args.linux_ci_log_directory.iterdir()):
                    if not path.is_file() or path.is_symlink():
                        raise CandidateError("Linux CI log directory contains an unsafe entry")
                    shutil.copyfile(path, target_logs / path.name)
            manifest_sha = sha256_file(stage / "candidate-manifest.json")
            os.replace(stage, final)
        print(
            json.dumps(
                {
                    "candidate": str(final),
                    "revision": revision,
                    "archive_sha256": manifest["source"]["archive"]["sha256"],
                    "manifest_sha256": manifest_sha,
                    "production_qualified": qualification["production_qualified"],
                },
                sort_keys=True,
            )
        )
        return 0
    except (
        CandidateError,
        ReportError,
        SingleSeatEvidenceError,
        CapacityEvidenceError,
        LinuxCIEvidenceError,
        CrossMachineEvidenceError,
        FaultRecoveryEvidenceError,
        TeacherInstallEvidenceError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
