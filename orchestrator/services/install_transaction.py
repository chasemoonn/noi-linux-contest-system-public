"""Durable fail-closed state machine for the V1 service install transaction."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import tempfile
from typing import Callable, Protocol


PHASES = (
    "source_release",
    "controller_quiesce",
    "hydro_integration",
    "closed_frontend",
    "controller",
    "post_install_verification",
)

# Rollback is deliberately not the mechanical reverse of PHASES.  The public
# Caddy hardening belongs to ``closed_frontend``.  Restoring it before the
# Hydro add-on would briefly make the add-on routes public again.  Stop the
# controller first, restore the private Hydro baseline, and only then restore
# the former Caddy configuration.
ROLLBACK_ORDER = (
    "post_install_verification",
    "controller",
    "hydro_integration",
    "closed_frontend",
    "controller_quiesce",
    "source_release",
)
CLEAN_PHASES = (
    "source_release", "clean_materials", "hydro_integration",
    "closed_frontend", "controller", "post_install_verification",
)
CLEAN_ROLLBACK_ORDER = (
    "post_install_verification", "controller", "hydro_integration",
    "closed_frontend", "clean_materials", "source_release",
)


class InstallTransactionError(RuntimeError):
    pass


class PhaseDriver(Protocol):
    def apply(self, context: "TransactionContext") -> dict: ...
    def rollback(self, context: "TransactionContext", receipt: dict | None) -> dict: ...


class CommitCleanupDriver(Protocol):
    def commit_cleanup(self, context: "TransactionContext", receipt: dict) -> dict: ...


@dataclass(frozen=True)
class TransactionContext:
    plan_id: str
    backup_manifest_sha256: str
    transaction_directory: Path


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _fsync_directory(path: Path) -> None:
    if platform.system().lower() != "linux":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def _atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"): os.fchmod(descriptor, 0o600)
        else: os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1; output.write(canonical(value)); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path); _fsync_directory(path.parent)
    finally:
        if descriptor >= 0: os.close(descriptor)
        if os.path.lexists(temporary): os.unlink(temporary)


def _read_exact(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not __import__("stat").S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or not 0 < metadata.st_size <= 4 * 1024 * 1024:
            raise InstallTransactionError("transaction journal metadata is unsafe")
        raw = os.read(descriptor, metadata.st_size + 1)
        if len(raw) != metadata.st_size: raise InstallTransactionError("transaction journal changed while reading")
    finally: os.close(descriptor)
    try: value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallTransactionError("transaction journal is invalid") from exc
    if not isinstance(value, dict): raise InstallTransactionError("transaction journal is not an object")
    return value


def initial_journal(plan_id: str, backup_manifest_sha256: str, phases=PHASES) -> dict:
    if not __import__("re").fullmatch(r"[a-f0-9]{64}", plan_id) or not __import__("re").fullmatch(r"[a-f0-9]{64}", backup_manifest_sha256):
        raise InstallTransactionError("transaction identity is invalid")
    return {"schema_version": 1, "plan_id": plan_id,
            "backup_manifest_sha256": backup_manifest_sha256,
            "status": "applying", "next_phase": phases[0], "in_progress": None, "completed": [],
            "receipts": {}, "rollback_completed": [], "failure": None}


def validate_journal(row: dict, plan_id: str, backup_manifest_sha256: str,
                     phases=PHASES, rollback_order=ROLLBACK_ORDER) -> dict:
    fields = {"schema_version", "plan_id", "backup_manifest_sha256", "status", "next_phase", "in_progress",
              "completed", "receipts", "rollback_completed", "failure"}
    if set(row) != fields or row["schema_version"] != 1 or row["plan_id"] != plan_id \
            or row["backup_manifest_sha256"] != backup_manifest_sha256:
        raise InstallTransactionError("transaction journal identity differs")
    if row["status"] not in {"applying", "rolling_back", "rollback_verified", "manual_intervention", "committed"}:
        raise InstallTransactionError("transaction status differs")
    completed = row["completed"]
    if not isinstance(completed, list) or completed != list(phases[:len(completed)]) or set(row["receipts"]) != set(completed):
        raise InstallTransactionError("completed install phases are not an exact prefix")
    rollback = row["rollback_completed"]
    if not isinstance(rollback, list):
        raise InstallTransactionError("completed rollback phases differ")
    expected_next = phases[len(completed)] if len(completed) < len(phases) else None
    if row["status"] == "applying" and row["next_phase"] != expected_next:
        raise InstallTransactionError("next install phase differs")
    if row["in_progress"] is not None and row["in_progress"] != expected_next:
        raise InstallTransactionError("in-progress install phase differs")
    rollback_scope = set(completed)
    if row["in_progress"] is not None:
        rollback_scope.add(row["in_progress"])
    rollback_targets = [phase for phase in rollback_order if phase in rollback_scope]
    if rollback != rollback_targets[:len(rollback)]:
        raise InstallTransactionError("completed rollback phases differ from durable intents")
    if not isinstance(row["receipts"], dict) or any(not isinstance(value, dict) for value in row["receipts"].values()):
        raise InstallTransactionError("phase receipt differs")
    return row


def _receipt(phase: str, action: str, value: dict) -> dict:
    if not isinstance(value, dict) or value.get("phase") != phase or value.get("action") != action \
            or value.get("status") != "verified" or set(value) != {"phase", "action", "status", "evidence_sha256"} \
            or not __import__("re").fullmatch(r"[a-f0-9]{64}", str(value.get("evidence_sha256"))):
        raise InstallTransactionError(f"{phase} {action} receipt differs")
    return value


def _final_path(root: Path, status: str, plan_id: str) -> Path:
    return root / f"service-install.{status}-{plan_id}.json"


def _finalize(root: Path, pending: Path, journal: dict, status: str) -> dict:
    journal["status"] = status
    # Seal the pending journal to the exact terminal bytes first.  A crash
    # after this write can safely recreate or verify the final receipt.
    _atomic_json(pending, journal)
    final = _final_path(root, status, journal["plan_id"])
    if os.path.lexists(final):
        if _read_exact(final) != journal:
            raise InstallTransactionError("durable service transaction receipt differs")
    else:
        _atomic_json(final, journal)
    if os.path.lexists(pending):
        os.unlink(pending); _fsync_directory(root)
    return journal


def _existing_final(root: Path, plan_id: str, backup_manifest_sha256: str,
                    phases=PHASES, rollback_order=ROLLBACK_ORDER) -> dict | None:
    found = []
    for status in ("committed", "rollback_verified"):
        path = _final_path(root, status, plan_id)
        if not os.path.lexists(path):
            continue
        row = validate_journal(_read_exact(path), plan_id, backup_manifest_sha256, phases, rollback_order)
        if row["status"] != status:
            raise InstallTransactionError("durable service transaction status differs")
        found.append(row)
    if len(found) > 1:
        raise InstallTransactionError("conflicting durable service transaction receipts")
    return found[0] if found else None


def _cleanup_path(root: Path, plan_id: str) -> Path:
    return root / f"service-install.cleanup-{plan_id}.json"


def _rollback_verification_path(root: Path, plan_id: str) -> Path:
    return root / f"service-install.rollback-verification-{plan_id}.json"


def _rollback_cleanup_path(root: Path, plan_id: str) -> Path:
    return root / f"service-install.rollback-cleanup-{plan_id}.json"


def _verify_and_cleanup_rollback(root: Path, context: TransactionContext,
                                 verify_rollback: Callable[[TransactionContext], dict]) -> None:
    expected = {"status": "rollback_verified", "plan_id": context.plan_id,
                "backup_manifest_sha256": context.backup_manifest_sha256}
    verification = _rollback_verification_path(root, context.plan_id)
    if os.path.lexists(verification):
        if _read_exact(verification) != expected:
            raise InstallTransactionError("durable rollback verification differs")
    else:
        if verify_rollback(context) != expected:
            raise InstallTransactionError("final rollback verification differs")
        _atomic_json(verification, expected)
    cleanup = getattr(verify_rollback, "rollback_cleanup", None)
    if cleanup is None:
        return
    cleanup_path = _rollback_cleanup_path(root, context.plan_id)
    cleanup_receipt = {"schema_version": 1, "plan_id": context.plan_id,
                       "backup_manifest_sha256": context.backup_manifest_sha256,
                       "status": "verified"}
    if os.path.lexists(cleanup_path):
        if _read_exact(cleanup_path) != cleanup_receipt:
            raise InstallTransactionError("durable rollback cleanup differs")
        return
    if cleanup(context) != {"phase": "final_rollback", "action": "cleanup", "status": "verified"}:
        raise InstallTransactionError("final rollback cleanup differs")
    _atomic_json(cleanup_path, cleanup_receipt)


def _commit_cleanup(root: Path, context: TransactionContext, journal: dict,
                    drivers: dict[str, PhaseDriver], phases=PHASES) -> None:
    path = _cleanup_path(root, context.plan_id)
    if os.path.lexists(path):
        value = _read_exact(path)
        expected = {"schema_version": 1, "plan_id": context.plan_id,
                    "backup_manifest_sha256": context.backup_manifest_sha256,
                    "status": "verified", "phases": list(phases)}
        if value != expected:
            raise InstallTransactionError("service install cleanup receipt differs")
        return
    completed = []
    for phase in phases:
        driver = drivers[phase]
        cleanup = getattr(driver, "commit_cleanup", None)
        if cleanup is not None:
            result = cleanup(context, journal["receipts"][phase])
            if result != {"phase": phase, "action": "commit_cleanup", "status": "verified"}:
                raise InstallTransactionError(f"{phase} commit cleanup differs")
        completed.append(phase)
    _atomic_json(path, {"schema_version": 1, "plan_id": context.plan_id,
                        "backup_manifest_sha256": context.backup_manifest_sha256,
                        "status": "verified", "phases": completed})


def _safe_transaction_directory(path: Path) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    metadata = os.lstat(requested)
    if requested != resolved or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise InstallTransactionError("transaction directory is unsafe")
    if platform.system().lower() == "linux" and (
        metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise InstallTransactionError("transaction directory must be root-owned mode 0700")
    return resolved


def _lock(path: Path) -> int:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor); raise InstallTransactionError("transaction lock metadata is unsafe")
    if platform.system().lower() == "linux":
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
            os.close(descriptor); raise InstallTransactionError("transaction lock ownership differs")
        import fcntl
        try: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor); raise InstallTransactionError("another service install transaction is running") from exc
    return descriptor


def _run_locked(transaction_directory: Path, plan_id: str, backup_manifest_sha256: str,
                 drivers: dict[str, PhaseDriver], verify_rollback: Callable[[TransactionContext], dict],
                 phases=PHASES, rollback_order=ROLLBACK_ORDER,
                 after_phase_committed: Callable[[TransactionContext, str, dict], None] | None = None) -> dict:
    if set(drivers) != set(phases):
        raise InstallTransactionError("install phase driver set differs")
    journal_path = transaction_directory / "service-install.pending.json"
    context = TransactionContext(plan_id, backup_manifest_sha256, transaction_directory)
    final = _existing_final(transaction_directory, plan_id, backup_manifest_sha256, phases, rollback_order)
    if final is not None:
        if os.path.lexists(journal_path):
            pending = validate_journal(_read_exact(journal_path), plan_id, backup_manifest_sha256, phases, rollback_order)
            if pending != final:
                raise InstallTransactionError("pending and durable final transaction differ")
            os.unlink(journal_path); _fsync_directory(transaction_directory)
        if final["status"] == "committed":
            _commit_cleanup(transaction_directory, context, final, drivers, phases)
        return final
    if os.path.lexists(journal_path):
        journal = validate_journal(_read_exact(journal_path), plan_id, backup_manifest_sha256, phases, rollback_order)
        if journal["status"] in {"committed", "rollback_verified"}:
            return _finalize(transaction_directory, journal_path, journal, journal["status"])
        if journal["status"] == "manual_intervention":
            raise InstallTransactionError("transaction requires manual intervention")
        # A prior process may have died inside a non-idempotent apply call.
        # Never continue forward from a pre-existing journal.
        if journal["status"] == "applying":
            journal["status"] = "rolling_back"; journal["failure"] = "interrupted_apply"
            _atomic_json(journal_path, journal)
    else:
        journal = initial_journal(plan_id, backup_manifest_sha256, phases)
        _atomic_json(journal_path, journal)
        try:
            for phase in phases:
                journal["in_progress"] = phase; _atomic_json(journal_path, journal)
                receipt = _receipt(phase, "apply", drivers[phase].apply(context))
                journal["completed"].append(phase); journal["receipts"][phase] = receipt; journal["in_progress"] = None
                journal["next_phase"] = phases[len(journal["completed"])] if len(journal["completed"]) < len(phases) else None
                _atomic_json(journal_path, journal)
                # Qualification uses this boundary to inject a normal failure
                # or SIGKILL only after the phase receipt and journal are
                # durable.  Production upgrade does not expose this callback.
                if after_phase_committed is not None:
                    after_phase_committed(context, phase, receipt)
        except BaseException as exc:
            journal["status"] = "rolling_back"; journal["failure"] = type(exc).__name__
            _atomic_json(journal_path, journal)
        else:
            # The terminal commit is the point of no return.  Cleanup happens
            # outside the apply exception boundary so an interrupted cleanup
            # is retried and can never roll back an already committed system.
            committed = _finalize(transaction_directory, journal_path, journal, "committed")
            _commit_cleanup(transaction_directory, context, committed, drivers, phases)
            return committed
    try:
        rollback_scope = set(journal["completed"])
        if journal["in_progress"] is not None:
            rollback_scope.add(journal["in_progress"])
        targets = [phase for phase in rollback_order if phase in rollback_scope]
        already = set(journal["rollback_completed"])
        for phase in targets:
            if phase in already: continue
            _receipt(phase, "rollback", drivers[phase].rollback(context, journal["receipts"].get(phase)))
            journal["rollback_completed"].append(phase); _atomic_json(journal_path, journal)
        # Verify while the immutable candidate release still exists.  The
        # verification receipt is durable before optional clean-install source
        # cleanup, so a crash inside cleanup can resume without needing to
        # recreate or execute a partially removed release tree.
        _verify_and_cleanup_rollback(transaction_directory, context, verify_rollback)
        return _finalize(transaction_directory, journal_path, journal, "rollback_verified")
    except BaseException as exc:
        # A cleanup failure after a durable live verification is retryable and
        # must not strand the transaction in manual_intervention.  All earlier
        # rollback/verification failures remain fail-closed and manual.
        if os.path.lexists(_rollback_verification_path(transaction_directory, plan_id)):
            journal["status"] = "rolling_back"
        else:
            journal["status"] = "manual_intervention"
        journal["failure"] = type(exc).__name__
        _atomic_json(journal_path, journal); raise


def run(transaction_directory: Path, plan_id: str, backup_manifest_sha256: str,
        drivers: dict[str, PhaseDriver], verify_rollback: Callable[[TransactionContext], dict]) -> dict:
    root = _safe_transaction_directory(transaction_directory)
    descriptor = _lock(root / "service-install.lock")
    try:
        return _run_locked(root, plan_id, backup_manifest_sha256, drivers, verify_rollback)
    finally:
        os.close(descriptor)


def run_clean(transaction_directory: Path, plan_id: str, backup_manifest_sha256: str,
              drivers: dict[str, PhaseDriver], verify_rollback: Callable[[TransactionContext], dict], *,
              after_phase_committed: Callable[[TransactionContext, str, dict], None] | None = None) -> dict:
    root = _safe_transaction_directory(transaction_directory)
    descriptor = _lock(root / "service-install.lock")
    try:
        return _run_locked(root, plan_id, backup_manifest_sha256, drivers, verify_rollback,
                           CLEAN_PHASES, CLEAN_ROLLBACK_ORDER, after_phase_committed)
    finally:
        os.close(descriptor)
