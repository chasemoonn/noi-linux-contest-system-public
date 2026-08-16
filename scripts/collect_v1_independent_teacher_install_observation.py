#!/usr/bin/env python3
"""Seal a machine-produced independent-teacher install/rollback observation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import apply_v1_clean_install as clean
import build_v1_clean_install_rehearsal_matrix as matrix_builder
import build_v1_independent_teacher_install_evidence as evidence_builder
import collect_v1_clean_install_rehearsal_observation as case_observation
from orchestrator.services import install_transaction as transaction
from verify_v1_clean_install_rehearsal import RehearsalError, validate as validate_matrix
from verify_v1_independent_teacher_install import MARKER, EvidenceError, identity
from verify_v1_install_backup import safe_directory


CONFIRMATION = "INDEPENDENT-TEACHER-CLEAN-INSTALL-AND-ROLLBACK"
ARTIFACTS = {
    "install_log": "install.log",
    "rollback_receipt": "rollback-receipt.json",
    "ordinary_oj_before": "ordinary-oj-before.json",
    "ordinary_oj_after": "ordinary-oj-after.json",
    "clean_install_rehearsal": "clean-install-rehearsal.json",
}
OPERATOR = re.compile(r"^[A-Za-z0-9_.@+-]{1,128}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")


class CollectionError(RuntimeError):
    pass


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def trusted_self() -> None:
    requested = Path(os.path.abspath(__file__)); metadata = os.lstat(requested)
    if requested != requested.resolve(strict=True) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CollectionError("teacher observation collector metadata is unsafe")
    if platform.system().lower() == "linux":
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise CollectionError("teacher observation collector is not trusted")
        trusted_ancestors(requested.parent, "teacher observation collector")


def trusted_ancestors(path: Path, label: str) -> None:
    current = Path(os.path.abspath(path)).resolve(strict=True)
    while True:
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) \
                or platform.system().lower() == "linux" and (
                    metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022):
            raise CollectionError(f"{label} ancestor is unsafe")
        if current.parent == current:
            return
        current = current.parent


def private_directory(path: Path, label: str) -> Path:
    try: directory = safe_directory(Path(os.path.abspath(path)))
    except (OSError, ValueError) as exc:
        raise CollectionError(f"{label} directory is unsafe") from exc
    if platform.system().lower() == "linux":
        metadata = os.lstat(directory)
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CollectionError(f"{label} directory must be root-only")
        trusted_ancestors(directory.parent, label)
    return directory


def private_json(path: Path, label: str) -> tuple[dict, bytes]:
    try: value, raw = evidence_builder.read_json(path, label)
    except (evidence_builder.BuildError, OSError) as exc:
        raise CollectionError(f"{label} could not be read safely") from exc
    return value, raw


def operator_identity(path: Path) -> str:
    try: raw = evidence_builder.private_file(path, "teacher operator identity", limit=4096)
    except (evidence_builder.BuildError, OSError) as exc:
        raise CollectionError("teacher operator identity could not be read safely") from exc
    try: value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise CollectionError("teacher operator identity is not UTF-8") from exc
    if not OPERATOR.fullmatch(value) or raw.strip() != value.encode("utf-8"):
        raise CollectionError("teacher operator identity format differs")
    return hashlib.sha256(raw).hexdigest()


def current_host(matrix: dict, qualification_marker: str, *, machine: bytes | None = None,
                 release: bytes | None = None) -> dict:
    machine = matrix_builder.system_file(Path("/etc/machine-id"), 4096).strip() if machine is None else machine.strip()
    release = matrix_builder.system_file(Path("/etc/os-release"), 1024 * 1024) if release is None else release
    if not machine:
        raise CollectionError("teacher qualification machine identity is empty")
    matrix_host = hashlib.sha256(matrix["session_id"].encode("ascii") + b":" + machine).hexdigest()
    if matrix_host == matrix["host"]["anonymous_id"]:
        raise CollectionError("teacher qualification must run on a different machine from the rehearsal matrix")
    return {
        "anonymous_id": hashlib.sha256(qualification_marker.encode("ascii") + b":" + machine).hexdigest(),
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "os_release_sha256": hashlib.sha256(release).hexdigest(),
    }


def load_teacher_case(directory: Path, plan: dict, expected_plan_sha256: str) -> tuple[dict, dict]:
    case_root = private_directory(directory, "teacher rehearsal case")
    value, _ = private_json(case_root / "observation.json", "teacher rehearsal observation")
    try: case_observation.validate_document(value, case_root, plan)
    except (case_observation.ObservationError, OSError, ValueError) as exc:
        raise CollectionError("teacher rehearsal observation differs") from exc
    if value["kind"] != "phase_failure" or value["phase"] != "post_install_verification" \
            or value["private_plan_sha256"] != expected_plan_sha256:
        raise CollectionError("teacher rehearsal must cover the final post-install failure boundary")
    receipt_name = value["artifacts"]["terminal_receipt"]["filename"]
    receipt, _ = private_json(case_root / receipt_name, "teacher rollback receipt")
    try:
        case_observation.validate_terminal_journal(
            receipt, plan, "phase_failure", "post_install_verification"
        )
    except (case_observation.ObservationError, transaction.InstallTransactionError) as exc:
        raise CollectionError("teacher rollback receipt differs") from exc
    receipts = receipt["receipts"]
    receipts_valid = isinstance(receipts, dict) and set(receipts) == set(transaction.CLEAN_PHASES)
    if receipts_valid:
        for phase, row in receipts.items():
            if not isinstance(row, dict) or set(row) != {"phase", "action", "status", "evidence_sha256"} \
                    or row.get("phase") != phase or row.get("action") != "apply" \
                    or row.get("status") != "verified" or not HEX64.fullmatch(str(row.get("evidence_sha256"))):
                receipts_valid = False; break
    if receipt["completed"] != list(transaction.CLEAN_PHASES) or not receipts_valid:
        raise CollectionError("teacher install did not verify every clean-install phase")
    return value, receipt


def assemble(source: dict, components: dict, host_value: dict, marker: str,
             operator_sha256: str, observed_at: str) -> dict:
    document = {
        "$schema": "v1-independent-teacher-install-observation.schema.json",
        "schema_version": 1,
        "source": source,
        "components": components,
        "observed_at": observed_at,
        "host": host_value,
        "teacher": {"qualification_marker": marker, "independent": True,
                    "operator_id_sha256": operator_sha256},
        "checks": {"candidate_verified": True, "clean_target": True,
                   "root_only_staging": True, "closed_frontend": True,
                   "controller_healthy": True, "active_seats": 0, "managed_rules": 0,
                   "cloud_state": "STOPPED", "ordinary_oj_errors": 0,
                   "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0,
                   "rollback_verified": True, "pending_markers": 0},
        "artifacts": dict(ARTIFACTS),
    }
    try:
        identity(document)
        # Reuse the final evidence validator's exact semantic checks by wrapping
        # only the unsigned fields is intentionally avoided; these checks remain
        # explicit and are revalidated after the teacher signs the observation.
        if not MARKER.fullmatch(marker):
            raise EvidenceError("teacher marker differs")
    except EvidenceError as exc:
        raise CollectionError("teacher observation identity differs") from exc
    return document


def fsync_directory(path: Path) -> None:
    if platform.system().lower() != "linux":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def write_private(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if hasattr(os, "fchmod"): os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1; output.write(raw); output.flush(); os.fsync(output.fileno())
    finally:
        if descriptor >= 0: os.close(descriptor)


def publish(output: Path, document: dict, artifact_sources: dict[str, tuple[Path, bytes]]) -> Path:
    requested = Path(os.path.abspath(output)); parent = private_directory(requested.parent, "teacher output parent")
    if os.path.lexists(requested):
        raise CollectionError("teacher observation output already exists")
    staging = Path(tempfile.mkdtemp(prefix=f".{requested.name}.", dir=parent)); os.chmod(staging, 0o700)
    try:
        artifact_root = staging / "artifacts"; os.mkdir(artifact_root, 0o700)
        for name, filename in ARTIFACTS.items():
            source, expected = artifact_sources[name]
            try: observed = evidence_builder.private_file(source, f"teacher artifact {name}", limit=64 * 1024 * 1024)
            except (evidence_builder.BuildError, OSError) as exc:
                raise CollectionError(f"teacher artifact {name} changed") from exc
            if observed != expected:
                raise CollectionError(f"teacher artifact {name} changed")
            write_private(artifact_root / filename, observed)
        fsync_directory(artifact_root)
        write_private(staging / "teacher-install-observation.json", canonical(document))
        fsync_directory(staging)
        os.replace(staging, requested); fsync_directory(parent)
    finally:
        if os.path.lexists(staging): shutil.rmtree(staging)
    return requested


def collect(args) -> dict:
    if args.confirm_independent != CONFIRMATION or not MARKER.fullmatch(str(args.qualification_marker)):
        raise CollectionError("independent teacher confirmation differs")
    candidate = private_directory(args.candidate, "candidate")
    try: candidate_row = evidence_builder.verify_candidate(candidate, args.expected_manifest_sha256)
    except (evidence_builder.BuildError, OSError) as exc:
        raise CollectionError("candidate verification failed") from exc
    plan = clean.load_plan(args.private_plan, args.expected_plan_sha256)
    if plan.get("scope") != "qualification-lab" \
            or Path(plan["candidate"]).resolve(strict=True) != candidate \
            or plan["candidate_manifest_sha256"] != args.expected_manifest_sha256:
        raise CollectionError("teacher clean-install plan does not bind the candidate")
    expected_release = f"{candidate_row['revision']}-{args.expected_manifest_sha256[:12]}"
    if plan["source_release"] != expected_release:
        raise CollectionError("teacher clean-install source release differs")

    matrix, matrix_raw = private_json(args.clean_install_rehearsal, "clean install rehearsal matrix")
    try: validate_matrix(matrix, expected_revision=candidate_row["revision"], expected_tree=candidate_row["tree"])
    except RehearsalError as exc:
        raise CollectionError("clean install rehearsal matrix differs") from exc
    case, _receipt = load_teacher_case(args.teacher_case_directory, plan, args.expected_plan_sha256)
    result = case["result"]
    if result != {"terminal": "rollback_verified", "clean_target": True, "caddy_restored": True,
            "hydro_restored": True, "controller_absent": True, "cloud_state": "STOPPED",
            "pending_markers": 0, "ordinary_oj_errors": 0, "ordinary_oj_restarts": 0,
            "ordinary_oj_pid_changes": 0}:
        raise CollectionError("teacher rehearsal rollback result differs")

    case_root = private_directory(args.teacher_case_directory, "teacher rehearsal case")
    artifact_sources = {
        "install_log": (case_root / case["artifacts"]["execution_log"]["filename"], b""),
        "rollback_receipt": (case_root / case["artifacts"]["terminal_receipt"]["filename"], b""),
        "ordinary_oj_before": (case_root / case["artifacts"]["ordinary_before"]["filename"], b""),
        "ordinary_oj_after": (case_root / case["artifacts"]["ordinary_after"]["filename"], b""),
        "clean_install_rehearsal": (Path(os.path.abspath(args.clean_install_rehearsal)), matrix_raw),
    }
    for name, (path, expected) in list(artifact_sources.items()):
        if expected:
            continue
        try: raw = evidence_builder.private_file(path, f"teacher artifact {name}", limit=64 * 1024 * 1024)
        except (evidence_builder.BuildError, OSError) as exc:
            raise CollectionError(f"teacher artifact {name} could not be read safely") from exc
        artifact_sources[name] = (path, raw)
    if artifact_sources["ordinary_oj_before"][1] != artifact_sources["ordinary_oj_after"][1]:
        raise CollectionError("teacher ordinary OJ before/after bytes differ")

    marker = args.qualification_marker
    document = assemble({"revision": candidate_row["revision"], "tree": candidate_row["tree"]},
        matrix["components"], current_host(matrix, marker), marker,
        operator_identity(args.operator_id_file),
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    output = publish(args.output_directory, document, artifact_sources)
    # Re-read the final directory using the same builder-facing contract.
    observed, raw = private_json(output / "teacher-install-observation.json", "teacher install observation")
    if observed != document:
        raise CollectionError("published teacher observation differs")
    hashes = evidence_builder.artifact_hashes(output / "artifacts", observed["artifacts"],
        expected_source=observed["source"], expected_components=observed["components"])
    return {"status": "sealed", "observation_sha256": hashlib.sha256(raw).hexdigest(),
            "artifact_hashes": hashes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--private-plan", required=True, type=Path)
    parser.add_argument("--expected-plan-sha256", required=True)
    parser.add_argument("--clean-install-rehearsal", required=True, type=Path)
    parser.add_argument("--teacher-case-directory", required=True, type=Path)
    parser.add_argument("--operator-id-file", required=True, type=Path)
    parser.add_argument("--qualification-marker", required=True)
    parser.add_argument("--confirm-independent", required=True)
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise CollectionError("teacher observation collection requires Linux root")
        trusted_self(); clean.trusted_self()
        print(json.dumps(collect(args), sort_keys=True)); return 0
    except (CollectionError, clean.ApplyInstallError, evidence_builder.BuildError,
            EvidenceError, RehearsalError, OSError, ValueError, UnicodeDecodeError,
            json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
