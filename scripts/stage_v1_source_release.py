#!/usr/bin/env python3
"""Atomically stage one externally pinned, production-qualified V1 source release.

This transaction deliberately does not run installers or touch Docker, PM2,
Caddy, databases, Hydro, or cloud resources.  It establishes the trusted source
release and durable recovery boundary that a later service transaction consumes.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tarfile
import tempfile

import noictl


HEX64 = re.compile(r"^[a-f0-9]{64}$")
HEX40 = re.compile(r"^[a-f0-9]{40}$")
ROOT_NAME = "noi-linux-contest-system-v1"


class TransactionError(RuntimeError):
    pass


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode()


def fsync_directory(path: Path) -> None:
    if platform.system().lower() != "linux":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(canonical(value)); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.lexists(temporary):
            os.unlink(temporary)


def safe_existing_directory(path: Path, label: str, *, writable_for_group_or_world: bool = False) -> Path:
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    metadata = os.lstat(requested)
    if requested != resolved or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TransactionError(f"{label} must be a canonical directory")
    if platform.system().lower() == "linux" and (
        metadata.st_uid != 0 or (not writable_for_group_or_world and stat.S_IMODE(metadata.st_mode) & 0o022)
    ):
        raise TransactionError(f"{label} must be root-owned and not group/world writable")
    return resolved


def install_root_path(path: Path, *, create: bool) -> Path:
    requested = Path(os.path.abspath(path))
    try:
        os.lstat(requested)
    except FileNotFoundError:
        parent = safe_existing_directory(requested.parent, "install root parent")
        if create:
            os.mkdir(requested, 0o755); fsync_directory(parent)
            return safe_existing_directory(requested, "install root")
        return requested
    return safe_existing_directory(requested, "install root")


def safe_root(path: Path) -> Path:
    return safe_existing_directory(path, "install root")


def private_subdirectory(parent: Path, name: str, mode: int) -> Path:
    path = parent / name
    created = False
    try:
        os.mkdir(path, mode)
        created = True
    except FileExistsError:
        pass
    # mkdir applies the caller's umask.  Install/recovery entrypoints normally
    # use umask 077, so explicitly normalize only the inode this transaction
    # just created.  A pre-existing directory is never silently repaired.
    if created:
        os.chmod(path, mode)
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TransactionError(f"{name} is not a safe directory")
    if platform.system().lower() == "linux" and (
        metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise TransactionError(f"{name} metadata differs")
    return path


def open_lock(root: Path) -> int:
    directory = private_subdirectory(root, ".locks", 0o700)
    path = directory / "source-release.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        os.close(descriptor); raise TransactionError("source release lock metadata is unsafe")
    if platform.system().lower() == "linux" and (
        opened.st_uid != 0 or stat.S_IMODE(opened.st_mode) != 0o600
    ):
        os.close(descriptor); raise TransactionError("source release lock ownership differs")
    import fcntl
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor); raise TransactionError("another source release transaction is running") from exc
    return descriptor


def candidate_identity(candidate: Path, external_manifest_sha256: str, *, require_production: bool) -> tuple[dict, bytes, dict]:
    if not HEX64.fullmatch(external_manifest_sha256):
        raise TransactionError("external manifest SHA256 is invalid")
    verified = noictl._verify_install_candidate(
        str(candidate), require_production_qualified=require_production
    )
    if verified["manifest_sha256"] != external_manifest_sha256:
        raise TransactionError("candidate differs from the external manifest trust pin")
    raw = noictl._safe_candidate_file(candidate / "candidate-manifest.json", maximum=32 * 1024 * 1024)
    manifest = json.loads(raw.decode("utf-8"))
    archive_name = manifest["source"]["archive"]["name"]
    archive = noictl._safe_candidate_file(candidate / archive_name, maximum=256 * 1024 * 1024)
    return manifest, archive, verified


def plan(candidate: Path, external_manifest_sha256: str, install_root: Path,
         *, qualification_lab: bool, owner_plan_id: str | None = None) -> dict:
    if owner_plan_id is not None and not HEX64.fullmatch(owner_plan_id):
        raise TransactionError("source release owner plan ID is invalid")
    manifest, _, verified = candidate_identity(
        candidate, external_manifest_sha256, require_production=not qualification_lab
    )
    root = install_root_path(install_root, create=False)
    identity = {
        "schema_version": 1,
        "operation": "stage-v1-source-release",
        "scope": "qualification-lab" if qualification_lab else "production",
        "install_root": str(root),
        "revision": verified["revision"],
        "tree": verified["tree"],
        "manifest_sha256": verified["manifest_sha256"],
        "archive_sha256": verified["archive_sha256"],
        "owner_plan_id": owner_plan_id,
    }
    plan_id = hashlib.sha256(canonical(identity)).hexdigest()
    release_name = f"{verified['revision']}-{verified['manifest_sha256'][:12]}"
    return {"status": "planned", "changed": False, "plan_id": plan_id,
            "release_name": release_name, "identity": identity,
            "tracked_files": manifest["source"]["tracked_file_count"],
            "service_mutations": 0}


def expected_files(manifest: dict) -> tuple[dict[str, dict], set[str]]:
    rows = manifest["source"]["files"]
    expected: dict[str, dict] = {}
    directories = {ROOT_NAME}
    for row in rows:
        name = f"{ROOT_NAME}/{row['path']}"
        expected[name] = row
        parent = Path(name).parent
        while str(parent) not in {"", "."}:
            directories.add(parent.as_posix()); parent = parent.parent
    return expected, directories


def extract_exact(manifest: dict, archive: bytes, staging: Path) -> None:
    expected, directories = expected_files(manifest)
    observed: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            name = member.name.rstrip("/") if member.isdir() else member.name
            if member.isdir():
                if name not in directories:
                    raise TransactionError("source archive has an unexpected directory")
                continue
            if not member.isfile() or member.issym() or member.islnk() or name not in expected or name in observed:
                raise TransactionError("source archive has an unexpected entry")
            handle = bundle.extractfile(member); content = handle.read() if handle else b""
            row = expected[name]; mode = 0o755 if row["mode"] == "100755" else 0o644
            if len(content) != row["bytes"] or hashlib.sha256(content).hexdigest() != row["sha256"] or member.mode & 0o777 != mode:
                raise TransactionError("source archive member differs")
            target = staging / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), mode)
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(content); output.flush(); os.fsync(output.fileno())
            observed.add(name)
    if observed != set(expected):
        raise TransactionError("source archive is incomplete")
    for directory in sorted((item for item in staging.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        os.chmod(directory, 0o755); fsync_directory(directory)
    os.chmod(staging, 0o755)
    fsync_directory(staging)


def verify_release(manifest: dict, release: Path) -> None:
    expected, directories = expected_files(manifest)
    expected_relative = {name.split("/", 1)[1]: row for name, row in expected.items()}
    expected_relative_directories = {
        name.split("/", 1)[1] for name in directories if name != ROOT_NAME
    }
    release_metadata = os.lstat(release)
    if not stat.S_ISDIR(release_metadata.st_mode) or stat.S_ISLNK(release_metadata.st_mode) \
            or stat.S_IMODE(release_metadata.st_mode) != 0o755:
        raise TransactionError("existing release root metadata differs")
    observed: set[str] = set()
    for path in release.rglob("*"):
        relative = path.relative_to(release).as_posix()
        metadata = os.lstat(path)
        if stat.S_ISDIR(metadata.st_mode):
            if relative not in expected_relative_directories or stat.S_IMODE(metadata.st_mode) != 0o755:
                raise TransactionError("existing release directory mode differs")
            continue
        row = expected_relative.get(relative)
        if row is None or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise TransactionError("existing release contains an unexpected entry")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            raw = os.read(descriptor, row["bytes"] + 1)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise TransactionError("existing release file identity changed")
        finally:
            os.close(descriptor)
        mode = 0o755 if row["mode"] == "100755" else 0o644
        if len(raw) != row["bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"] or stat.S_IMODE(metadata.st_mode) != mode:
            raise TransactionError("existing release file differs")
        observed.add(relative)
    if observed != set(expected_relative):
        raise TransactionError("existing release is incomplete")


def pointer_target(root: Path) -> str | None:
    path = root / "current-source"
    if not os.path.lexists(path):
        return None
    metadata = os.lstat(path)
    if not stat.S_ISLNK(metadata.st_mode):
        raise TransactionError("current-source is not a symbolic link")
    target = os.readlink(path)
    if not re.fullmatch(r"source-releases/[a-f0-9]{40}-[a-f0-9]{12}", target):
        raise TransactionError("current-source target is unsafe")
    return target


def replace_pointer(root: Path, target: str | None) -> None:
    pointer = root / "current-source"
    temporary = root / f".current-source.{os.getpid()}.{os.urandom(6).hex()}"
    if target is None:
        if os.path.lexists(pointer):
            os.unlink(pointer); fsync_directory(root)
        return
    os.symlink(target, temporary)
    try:
        os.replace(temporary, pointer); fsync_directory(root)
    finally:
        if os.path.lexists(temporary): os.unlink(temporary)


def load_pending(path: Path, plan_id: str) -> dict:
    try:
        raw = noictl._safe_candidate_file(path, maximum=1024 * 1024)
        row = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, noictl.InstallPlanError) as exc:
        raise TransactionError("pending transaction is unreadable") from exc
    required = {"schema_version", "plan_id", "owner_plan_id", "scope", "phase", "release_name",
                "previous_pointer", "new_pointer"}
    if not isinstance(row, dict) or set(row) != required or row["schema_version"] != 1 or row["plan_id"] != plan_id:
        raise TransactionError("pending transaction identity differs")
    if row["owner_plan_id"] is not None and not HEX64.fullmatch(str(row["owner_plan_id"])):
        raise TransactionError("pending transaction owner differs")
    if row["phase"] not in {"prepared", "release_installed", "pointer_committed"}:
        raise TransactionError("pending transaction phase differs")
    return row


def committed_receipt(transactions: Path, row: dict) -> bool:
    path = transactions / f"source-install.committed-{row['plan_id']}.json"
    if not os.path.lexists(path):
        return False
    try:
        raw = noictl._safe_candidate_file(path, maximum=1024 * 1024)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, noictl.InstallPlanError) as exc:
        raise TransactionError("committed receipt is unreadable") from exc
    expected = {**row, "status": "committed"}
    if value != expected or row["phase"] != "pointer_committed":
        raise TransactionError("committed receipt differs from pending transaction")
    return True


def load_committed(transactions: Path, plan_id: str) -> dict:
    path = transactions / f"source-install.committed-{plan_id}.json"
    try:
        raw = noictl._safe_candidate_file(path, maximum=1024 * 1024)
        row = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, noictl.InstallPlanError) as exc:
        raise TransactionError("committed receipt is unreadable") from exc
    required = {"schema_version", "plan_id", "owner_plan_id", "scope", "phase", "release_name",
                "previous_pointer", "new_pointer", "status"}
    if not isinstance(row, dict) or set(row) != required or row["schema_version"] != 1 \
            or row["plan_id"] != plan_id or row["phase"] != "pointer_committed" \
            or row["status"] != "committed" or row["scope"] not in {"qualification-lab", "production"}:
        raise TransactionError("committed receipt identity differs")
    if row["owner_plan_id"] is not None and not HEX64.fullmatch(str(row["owner_plan_id"])):
        raise TransactionError("committed receipt owner differs")
    expected_pointer = f"source-releases/{row['release_name']}"
    if row["new_pointer"] != expected_pointer or not re.fullmatch(
            r"[a-f0-9]{40}-[a-f0-9]{12}", str(row["release_name"])):
        raise TransactionError("committed receipt release identity differs")
    if row["previous_pointer"] is not None and not re.fullmatch(
            r"source-releases/[a-f0-9]{40}-[a-f0-9]{12}", str(row["previous_pointer"])):
        raise TransactionError("committed receipt previous pointer differs")
    return row


def rollback_intent(row: dict) -> dict:
    return {
        "schema_version": 1,
        "operation": "rollback-committed-source-release",
        "plan_id": row["plan_id"],
        "committed_receipt_sha256": hashlib.sha256(canonical(row)).hexdigest(),
        "previous_pointer": row["previous_pointer"],
        "new_pointer": row["new_pointer"],
        "status": "rolling_back",
    }


def load_exact_json(path: Path, expected: dict, label: str) -> None:
    try:
        raw = noictl._safe_candidate_file(path, maximum=1024 * 1024)
        observed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, noictl.InstallPlanError) as exc:
        raise TransactionError(f"{label} is unreadable") from exc
    if observed != expected:
        raise TransactionError(f"{label} differs")


def rollback_committed(root: Path, plan_id: str) -> dict:
    """Restore the pointer owned by one committed source transaction.

    The release tree is retained as immutable evidence.  A separate durable
    rollback intent makes a crash after the pointer swap distinguishable from
    an unrelated pointer change, so reruns can finish without guessing.
    """
    transactions = private_subdirectory(root, ".transactions", 0o700)
    apply_pending = transactions / "source-install.pending.json"
    if os.path.lexists(apply_pending):
        raise TransactionError("unfinished source apply requires explicit recovery")
    committed = load_committed(transactions, plan_id)
    intent = rollback_intent(committed)
    pending = transactions / "source-rollback.pending.json"
    terminal = transactions / f"source-install.committed-rollback-{plan_id}.json"
    verified = {**intent, "status": "rollback_verified"}
    current = pointer_target(root)

    if os.path.lexists(terminal):
        load_exact_json(terminal, verified, "rollback receipt")
        if os.path.lexists(pending):
            load_exact_json(pending, intent, "rollback intent")
            if current != committed["previous_pointer"]:
                raise TransactionError("rolled back source pointer differs")
            os.unlink(pending); fsync_directory(transactions)
        elif current != committed["previous_pointer"]:
            raise TransactionError("rolled back source pointer differs")
        return {"status": "rollback_verified", "changed": False, "plan_id": plan_id,
                "release": committed["new_pointer"], "service_mutations": 0}

    if os.path.lexists(pending):
        load_exact_json(pending, intent, "rollback intent")
        if current not in {committed["new_pointer"], committed["previous_pointer"]}:
            raise TransactionError("current-source changed outside the rollback transaction")
    else:
        if current != committed["new_pointer"]:
            raise TransactionError("committed source release no longer owns current-source")
        atomic_json(pending, intent)

    if current == committed["new_pointer"]:
        replace_pointer(root, committed["previous_pointer"])
    elif current != committed["previous_pointer"]:
        raise TransactionError("current-source changed outside the rollback transaction")
    atomic_json(terminal, verified)
    os.unlink(pending); fsync_directory(transactions)
    return {"status": "rollback_verified", "changed": True, "plan_id": plan_id,
            "release": committed["new_pointer"], "service_mutations": 0}


def rollback_owned(root: Path, plan_id: str) -> dict:
    """Rollback either an interrupted apply or its durable committed result."""
    transactions = private_subdirectory(root, ".transactions", 0o700)
    pending = transactions / "source-install.pending.json"
    if os.path.lexists(pending):
        result = recover(root, plan_id)
        if result["status"] == "rollback_verified":
            return result
        if result["status"] != "commit_recovered":
            raise TransactionError("source apply recovery result differs")
    committed_path = transactions / f"source-install.committed-{plan_id}.json"
    if not os.path.lexists(committed_path):
        if os.path.lexists(transactions / "source-rollback.pending.json"):
            raise TransactionError("source rollback exists without its committed receipt")
        recovered_path = transactions / f"source-install.rollback-{plan_id}.json"
        if os.path.lexists(recovered_path):
            try:
                raw = noictl._safe_candidate_file(recovered_path, maximum=1024 * 1024)
                recovered = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, noictl.InstallPlanError) as exc:
                raise TransactionError("source apply rollback receipt is unreadable") from exc
            required = {"schema_version", "plan_id", "owner_plan_id", "scope", "phase", "release_name",
                        "previous_pointer", "new_pointer", "status"}
            if not isinstance(recovered, dict) or set(recovered) != required \
                    or recovered["schema_version"] != 1 or recovered["plan_id"] != plan_id \
                    or recovered["phase"] != "rollback_verified" or recovered["status"] != "rolled_back" \
                    or recovered["scope"] not in {"qualification-lab", "production"}:
                raise TransactionError("source apply rollback receipt differs")
            if recovered["owner_plan_id"] is not None and not HEX64.fullmatch(
                    str(recovered["owner_plan_id"])):
                raise TransactionError("source apply rollback owner differs")
            if pointer_target(root) != recovered["previous_pointer"]:
                raise TransactionError("rolled back source pointer differs")
            return {"status": "rollback_verified", "changed": False, "plan_id": plan_id,
                    "release": recovered["new_pointer"], "service_mutations": 0}
        return {"status": "rollback_verified", "changed": False, "plan_id": plan_id,
                "release": None, "service_mutations": 0}
    return rollback_committed(root, plan_id)


def recover(root: Path, plan_id: str) -> dict:
    transactions = private_subdirectory(root, ".transactions", 0o700)
    pending = transactions / "source-install.pending.json"
    row = load_pending(pending, plan_id)
    current = pointer_target(root)
    if committed_receipt(transactions, row):
        if current != row["new_pointer"]:
            raise TransactionError("durably committed source pointer differs")
        os.unlink(pending); fsync_directory(transactions)
        return {"status": "commit_recovered", "changed": False, "plan_id": plan_id,
                "release": row["new_pointer"], "service_mutations": 0}
    if current == row["new_pointer"]:
        replace_pointer(root, row["previous_pointer"])
    elif current != row["previous_pointer"]:
        raise TransactionError("current-source changed outside the pending transaction")
    receipt = {**row, "status": "rolled_back", "phase": "rollback_verified"}
    atomic_json(transactions / f"source-install.rollback-{plan_id}.json", receipt)
    os.unlink(pending); fsync_directory(transactions)
    return {"status": "rollback_verified", "changed": True, "plan_id": plan_id,
            "service_mutations": 0}


def apply(candidate: Path, external_sha: str, root: Path, expected_plan_id: str,
          *, qualification_lab: bool, owner_plan_id: str | None = None) -> dict:
    planned = plan(candidate, external_sha, root, qualification_lab=qualification_lab,
                   owner_plan_id=owner_plan_id)
    if planned["plan_id"] != expected_plan_id:
        raise TransactionError("plan ID differs after complete revalidation")
    root = install_root_path(root, create=True)
    releases = private_subdirectory(root, "source-releases", 0o755)
    transactions = private_subdirectory(root, ".transactions", 0o700)
    pending = transactions / "source-install.pending.json"
    if os.path.lexists(transactions / "source-rollback.pending.json"):
        raise TransactionError("an unfinished source rollback requires explicit recovery")
    if os.path.lexists(pending):
        raise TransactionError("an unfinished source transaction requires explicit recovery")
    terminal_paths = (
        transactions / f"source-install.committed-{expected_plan_id}.json",
        transactions / f"source-install.rollback-{expected_plan_id}.json",
        transactions / f"source-install.committed-rollback-{expected_plan_id}.json",
    )
    if any(os.path.lexists(path) for path in terminal_paths):
        raise TransactionError("source transaction already has a durable terminal receipt")
    final = releases / planned["release_name"]
    manifest, archive, _ = candidate_identity(
        candidate, external_sha, require_production=not qualification_lab
    )
    reuse_release = os.path.lexists(final)
    if reuse_release:
        if stat.S_ISLNK(os.lstat(final).st_mode) or not final.is_dir():
            raise TransactionError("release target is unsafe")
        verify_release(manifest, final)
    previous = pointer_target(root); new_pointer = f"source-releases/{planned['release_name']}"
    row = {"schema_version": 1, "plan_id": expected_plan_id,
           "owner_plan_id": owner_plan_id,
           "scope": "qualification-lab" if qualification_lab else "production", "phase": "prepared",
           "release_name": planned["release_name"], "previous_pointer": previous,
           "new_pointer": new_pointer}
    atomic_json(pending, row)
    staging = None if reuse_release else Path(tempfile.mkdtemp(prefix=".source-stage.", dir=releases))
    try:
        if staging is not None:
            extract_exact(manifest, archive, staging)
            os.replace(staging, final); fsync_directory(releases)
        row["phase"] = "release_installed"; atomic_json(pending, row)
        replace_pointer(root, new_pointer)
        row["phase"] = "pointer_committed"; atomic_json(pending, row)
        receipt = {**row, "status": "committed"}
        atomic_json(transactions / f"source-install.committed-{expected_plan_id}.json", receipt)
        os.unlink(pending); fsync_directory(transactions)
        return {"status": "committed", "changed": True, "plan_id": expected_plan_id,
                "release": new_pointer, "service_mutations": 0}
    except BaseException:
        if staging is not None and os.path.lexists(staging):
            # Keep a partially written staging tree as root-only evidence.  It
            # is never referenced by current-source and recovery remains exact.
            os.chmod(staging, 0o700)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--plan", action="store_true")
    operation.add_argument("--apply", action="store_true")
    operation.add_argument("--recover", action="store_true")
    operation.add_argument("--rollback-committed", action="store_true")
    operation.add_argument("--rollback-owned", action="store_true")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--install-root", type=Path, default=Path("/opt/noi-linux-contest-system"))
    parser.add_argument("--plan-id")
    parser.add_argument("--owner-plan-id")
    parser.add_argument("--qualification-lab", action="store_true",
                        help="allow an unqualified candidate only in an isolated qualification machine")
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux":
            raise TransactionError("source release transactions require Linux")
        if not (args.recover or args.rollback_committed or args.rollback_owned) and (
                args.candidate is None or args.expected_manifest_sha256 is None):
            raise TransactionError("candidate and external manifest SHA256 are required")
        if (args.apply or args.recover or args.rollback_committed or args.rollback_owned) and (
                os.geteuid() != 0 or not HEX64.fullmatch(str(args.plan_id))):
            raise TransactionError("apply/recover/rollback requires root and an exact plan ID")
        if args.owner_plan_id is not None and not HEX64.fullmatch(str(args.owner_plan_id)):
            raise TransactionError("owner plan ID is invalid")
        if (args.recover or args.rollback_committed or args.rollback_owned) and args.owner_plan_id is not None:
            raise TransactionError("recovery reads the owner plan ID from the durable receipt")
        if args.plan:
            result = plan(args.candidate, args.expected_manifest_sha256, args.install_root,
                          qualification_lab=args.qualification_lab, owner_plan_id=args.owner_plan_id)
        else:
            if args.recover or args.rollback_committed or args.rollback_owned:
                root = safe_root(args.install_root)
            else:
                # Recompute and compare before even creating an absent install
                # root.  apply() repeats the same validation after locking.
                before = plan(args.candidate, args.expected_manifest_sha256,
                              args.install_root, qualification_lab=args.qualification_lab,
                              owner_plan_id=args.owner_plan_id)
                if before["plan_id"] != args.plan_id:
                    raise TransactionError("plan ID differs before transaction setup")
                root = install_root_path(args.install_root, create=True)
            lock = open_lock(root)
            try:
                if args.recover:
                    result = recover(root, args.plan_id)
                elif args.rollback_committed:
                    result = rollback_committed(root, args.plan_id)
                elif args.rollback_owned:
                    result = rollback_owned(root, args.plan_id)
                else:
                    result = apply(args.candidate, args.expected_manifest_sha256, root, args.plan_id,
                                   qualification_lab=args.qualification_lab,
                                   owner_plan_id=args.owner_plan_id)
            finally:
                os.close(lock)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True)); return 0
    except (TransactionError, noictl.InstallPlanError, noictl.InstallQualificationError,
            OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
