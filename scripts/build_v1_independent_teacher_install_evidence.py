#!/usr/bin/env python3
"""Build and sign independent-teacher evidence from machine-produced artifacts."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.services import install_transaction as transaction
from verify_v1_independent_teacher_install import (
    EvidenceError,
    NAMESPACE,
    canonical,
    validate,
)
from verify_v1_clean_install_rehearsal import RehearsalError, validate as validate_clean_rehearsal
from verify_v1_ordinary_oj_install_backup import (
    OrdinaryBackupError,
    compare as compare_ordinary,
    validate as validate_ordinary,
)


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")


class BuildError(RuntimeError):
    pass


def private_file(path: Path, label: str, *, executable: bool = False, limit: int = 32 * 1024 * 1024) -> bytes:
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    if requested != resolved or path.is_symlink():
        raise BuildError(f"{label} must be canonical and not a symlink")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limit:
        raise BuildError(f"{label} metadata is unsafe")
    if platform.system().lower() == "linux" and (
        info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise BuildError(f"{label} must be root-owned mode 0600 or stricter")
    if executable and not os.access(resolved, os.X_OK):
        raise BuildError(f"{label} is not executable")
    descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, opened.st_size + 1)
        if len(raw) != opened.st_size or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise BuildError(f"{label} changed while reading")
        return raw
    finally:
        os.close(descriptor)


def private_directory(path: Path, label: str) -> Path:
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    if requested != resolved or not resolved.is_dir() or resolved.is_symlink():
        raise BuildError(f"{label} must be a canonical directory")
    info = resolved.stat()
    if platform.system().lower() == "linux" and (
        info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise BuildError(f"{label} must be root-owned mode 0700 or stricter")
    return resolved


def read_json(path: Path, label: str) -> tuple[dict, bytes]:
    raw = private_file(path, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise BuildError(f"{label} must be an object")
    return value, raw


def verify_candidate(candidate: Path, expected_manifest_sha256: str) -> dict:
    manifest_path = candidate / "candidate-manifest.json"
    manifest_raw = private_file(manifest_path, "candidate manifest")
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    if not HEX64.fullmatch(str(expected_manifest_sha256)) or manifest_sha256 != expected_manifest_sha256:
        raise BuildError("candidate manifest differs from the external trust pin")
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
        archive_name = manifest["source"]["archive"]["name"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BuildError("candidate manifest is invalid") from exc
    if not isinstance(archive_name, str) or Path(archive_name).name != archive_name:
        raise BuildError("candidate archive name is unsafe")
    archive_raw = private_file(candidate / archive_name, "candidate archive", limit=512 * 1024 * 1024)
    source = manifest.get("source")
    files = source.get("files") if isinstance(source, dict) else None
    if not isinstance(files, list) or not files or source.get("tracked_file_count") != len(files):
        raise BuildError("candidate source file manifest is invalid")
    expected_files: dict[str, dict] = {}
    expected_directories = {"noi-linux-contest-system-v1"}
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "mode", "bytes", "sha256"}:
            raise BuildError("candidate source file row differs")
        relative = row["path"]
        if not isinstance(relative, str) or not relative or "\\" in relative or relative.startswith("/") or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise BuildError("candidate source path is unsafe")
        member_name = f"noi-linux-contest-system-v1/{relative}"
        if member_name in expected_files or row["mode"] not in {"100644", "100755"} or not isinstance(row["bytes"], int) or row["bytes"] < 0 or not HEX64.fullmatch(str(row["sha256"])):
            raise BuildError("candidate source metadata is invalid")
        expected_files[member_name] = row
        parent = Path(member_name).parent
        while str(parent) not in {"", "."}:
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    observed: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="noi-v1-teacher-candidate-") as raw:
        trusted_root = Path(raw)
        try:
            with tarfile.open(fileobj=io.BytesIO(archive_raw), mode="r:") as bundle:
                for member in bundle.getmembers():
                    name = member.name.rstrip("/") if member.isdir() else member.name
                    if member.isdir():
                        if name not in expected_directories:
                            raise BuildError("candidate archive contains an unexpected directory")
                        continue
                    if not member.isfile() or member.issym() or member.islnk() or name not in expected_files or name in observed:
                        raise BuildError("candidate archive contains an unexpected entry")
                    handle = bundle.extractfile(member)
                    content = handle.read() if handle is not None else b""
                    row = expected_files[name]
                    mode = 0o755 if row["mode"] == "100755" else 0o644
                    if len(content) != row["bytes"] or hashlib.sha256(content).hexdigest() != row["sha256"] or member.mode & 0o777 != mode:
                        raise BuildError("candidate archive member differs")
                    target = trusted_root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    target.chmod(mode)
                    observed.add(name)
        except (tarfile.TarError, OSError) as exc:
            raise BuildError("candidate archive is invalid") from exc
        if observed != set(expected_files):
            raise BuildError("candidate archive is incomplete")
        source_root = trusted_root / "noi-linux-contest-system-v1"
        verifier = source_root / "scripts" / "verify_v1_candidate.py"
        if not verifier.is_file() or verifier.is_symlink():
            raise BuildError("trusted candidate verifier is missing")
        result = subprocess.run(
            [sys.executable, str(verifier), str(candidate)], cwd=source_root,
            capture_output=True, text=True, timeout=120, check=False,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if result.returncode:
            raise BuildError("candidate did not pass complete verification")
        try:
            verified = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BuildError("candidate verifier output is invalid") from exc
    expected = {
        "manifest_sha256": manifest_sha256,
        "archive_sha256": hashlib.sha256(archive_raw).hexdigest(),
        "revision": manifest["source"]["revision"],
        "tree": manifest["source"]["tree"],
    }
    if verified.get("revision") != expected["revision"] or verified.get("archive_sha256") != expected["archive_sha256"] or verified.get("manifest_sha256") != expected["manifest_sha256"]:
        raise BuildError("candidate verifier identity differs")
    return expected


def artifact_hashes(root: Path, rows: dict, *, expected_source: dict | None = None,
                    expected_components: dict | None = None) -> dict:
    expected = {"install_log", "rollback_receipt", "ordinary_oj_before", "ordinary_oj_after",
                "clean_install_rehearsal"}
    if not isinstance(rows, dict) or set(rows) != expected:
        raise BuildError("teacher install artifact references differ")
    result = {}; loaded = {}
    for name in sorted(expected):
        reference = rows[name]
        if not isinstance(reference, str) or not SAFE_NAME.fullmatch(reference):
            raise BuildError("teacher install artifact reference is unsafe")
        raw = private_file(root / reference, f"teacher install artifact {name}")
        loaded[name] = raw
        if name == "clean_install_rehearsal" and expected_source is not None:
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BuildError("clean install rehearsal artifact is invalid JSON") from exc
            try:
                validate_clean_rehearsal(value, expected_revision=expected_source["revision"],
                    expected_tree=expected_source["tree"], expected_components=expected_components)
            except RehearsalError as exc:
                raise BuildError("clean install rehearsal artifact differs") from exc
        result[f"{name}_sha256"] = hashlib.sha256(raw).hexdigest()
    if result["ordinary_oj_before_sha256"] != result["ordinary_oj_after_sha256"]:
        raise BuildError("ordinary OJ before/after artifact bytes differ")
    validate_machine_artifacts(loaded)
    return result


def validate_machine_artifacts(artifacts: dict[str, bytes]) -> None:
    try:
        receipt = json.loads(artifacts["rollback_receipt"].decode("utf-8"))
        before = validate_ordinary(json.loads(artifacts["ordinary_oj_before"].decode("utf-8")))
        after = validate_ordinary(json.loads(artifacts["ordinary_oj_after"].decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, OrdinaryBackupError) as exc:
        raise BuildError("teacher install machine artifact is invalid") from exc
    if not isinstance(receipt, dict):
        raise BuildError("teacher rollback receipt root differs")
    plan_id = receipt.get("plan_id"); backup = receipt.get("backup_manifest_sha256")
    if not HEX64.fullmatch(str(plan_id)) or not HEX64.fullmatch(str(backup)):
        raise BuildError("teacher rollback receipt identity differs")
    try:
        transaction.validate_journal(receipt, plan_id, backup,
            transaction.CLEAN_PHASES, transaction.CLEAN_ROLLBACK_ORDER)
    except transaction.InstallTransactionError as exc:
        raise BuildError("teacher rollback receipt transaction differs") from exc
    expected_rollback = list(transaction.CLEAN_ROLLBACK_ORDER)
    if receipt["status"] != "rollback_verified" or receipt["completed"] != list(transaction.CLEAN_PHASES) \
            or receipt["rollback_completed"] != expected_rollback \
            or receipt["failure"] != "InjectedPhaseFailure" or receipt["next_phase"] is not None \
            or receipt["in_progress"] is not None:
        raise BuildError("teacher rollback receipt did not cover the complete install")
    for phase in transaction.CLEAN_PHASES:
        value = receipt["receipts"].get(phase)
        if not isinstance(value, dict) or set(value) != {"phase", "action", "status", "evidence_sha256"} \
                or value.get("phase") != phase or value.get("action") != "apply" \
                or value.get("status") != "verified" or not HEX64.fullmatch(str(value.get("evidence_sha256"))):
            raise BuildError("teacher install phase receipt differs")
    try:
        lines = [line for line in artifacts["install_log"].decode("utf-8").splitlines() if line]
        execution = json.loads(lines[0]) if len(lines) == 1 else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildError("teacher install execution log is invalid") from exc
    expected_execution = {"status": "passed", "mode": "phase-failure",
        "phase": "post_install_verification", "terminal": "rollback_verified",
        "plan_id": plan_id, "backup_manifest_sha256": backup}
    if execution != expected_execution:
        raise BuildError("teacher install execution log differs")
    compare_ordinary(before, after)


def atomic_write(path: Path, raw: bytes) -> None:
    parent = private_directory(path.parent, "evidence output directory")
    if os.path.lexists(path):
        raise BuildError("evidence output already exists")
    descriptor, temporary = tempfile.mkstemp(prefix=".teacher-install.", dir=parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def build(args) -> dict:
    if platform.system().lower() != "linux" or os.geteuid() != 0:
        raise BuildError("teacher install evidence must be built by root on Linux")
    observation, _ = read_json(args.observation, "teacher install observation")
    if set(observation) != {"$schema", "schema_version", "source", "components", "observed_at", "host", "teacher", "checks", "artifacts"} or observation["$schema"] != "v1-independent-teacher-install-observation.schema.json" or observation["schema_version"] != 1:
        raise BuildError("teacher install observation shape differs")
    candidate = private_directory(args.candidate, "candidate directory")
    candidate_row = verify_candidate(candidate, args.expected_manifest_sha256)
    if observation["source"] != {"revision": candidate_row["revision"], "tree": candidate_row["tree"]}:
        raise BuildError("teacher observation source differs from candidate")
    artifacts = artifact_hashes(private_directory(args.artifact_directory, "artifact directory"), observation["artifacts"],
                                expected_source=observation["source"], expected_components=observation["components"])
    key = Path(os.path.abspath(args.signing_key)).resolve(strict=True)
    private_file(key, "teacher signing key")
    ssh_keygen = Path(os.path.abspath(args.ssh_keygen)).resolve(strict=True)
    private_file(ssh_keygen, "ssh-keygen", executable=True)
    public = subprocess.run([str(ssh_keygen), "-y", "-f", str(key)], capture_output=True, text=True, timeout=10, check=False)
    if public.returncode or public.stdout.strip() != args.signing_public_key.strip():
        raise BuildError("teacher signing key does not match the public key")
    evidence = {"$schema": "v1-independent-teacher-install-evidence.schema.json", "schema_version": 1,
        "source": observation["source"], "components": observation["components"],
        "candidate": {"manifest_sha256": candidate_row["manifest_sha256"], "archive_sha256": candidate_row["archive_sha256"]},
        "observed_at": observation["observed_at"], "host": observation["host"], "teacher": observation["teacher"],
        "checks": observation["checks"], "artifacts": artifacts, "signer": args.signer,
        "signing_public_key": args.signing_public_key.strip()}
    with tempfile.TemporaryDirectory(prefix="noi-v1-teacher-sign-") as raw:
        payload = Path(raw) / "evidence.json"; payload.write_bytes(canonical(evidence)); os.chmod(payload, 0o600)
        signed = subprocess.run([str(ssh_keygen), "-q", "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(payload)], capture_output=True, timeout=10, check=False)
        signature = Path(str(payload) + ".sig")
        if signed.returncode or not signature.is_file(): raise BuildError("teacher evidence signing failed")
        evidence["signature"] = base64.b64encode(signature.read_bytes()).decode()
    validate(evidence, expected_revision=candidate_row["revision"], expected_tree=candidate_row["tree"],
             expected_archive_sha256=candidate_row["archive_sha256"], ssh_keygen=ssh_keygen)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", required=True, type=Path)
    parser.add_argument("--artifact-directory", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--signer", required=True)
    parser.add_argument("--signing-key", required=True, type=Path)
    parser.add_argument("--signing-public-key", required=True)
    parser.add_argument("--ssh-keygen", default="/usr/bin/ssh-keygen", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        evidence = build(args); raw = canonical(evidence); atomic_write(args.output, raw)
        print(json.dumps({"evidence_sha256": hashlib.sha256(raw).hexdigest(), "status": "passed"}, sort_keys=True)); return 0
    except (BuildError, EvidenceError, OSError, subprocess.SubprocessError) as exc:
        print(f"NO_GO: {exc}", file=__import__("sys").stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
