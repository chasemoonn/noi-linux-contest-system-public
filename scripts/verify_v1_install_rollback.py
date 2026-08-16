#!/usr/bin/env python3
"""Verify restored service artifacts exactly match one sealed install backup."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys
import tempfile

from verify_v1_install_backup import (
    BackupError, OPTIONAL, REQUIRED, safe_directory, safe_file, validate_manifest,
)


class RollbackError(RuntimeError):
    pass


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def compare(backup: Path, restored: Path, plan_id: str) -> tuple[dict, bytes]:
    backup_root = safe_directory(backup); restored_root = safe_directory(restored)
    manifest_raw, _ = safe_file(backup_root / "backup-manifest.json", maximum=4 * 1024 * 1024)
    try: manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RollbackError("backup manifest is invalid JSON") from exc
    validate_manifest(manifest, backup_root, expected_plan_id=plan_id)
    artifacts = manifest["artifacts"]
    expected_names = set()
    for name, filename in {**REQUIRED, **OPTIONAL}.items():
        baseline = artifacts[name]; path = restored_root / filename
        if not baseline["present"]:
            if os.path.lexists(path):
                raise RollbackError(f"restored optional artifact {name} should be absent")
            continue
        expected_names.add(filename)
        try: raw, metadata = safe_file(path)
        except FileNotFoundError as exc:
            raise RollbackError(f"restored artifact {name} is missing") from exc
        if len(raw) != baseline["bytes"] or stat.S_IMODE(metadata.st_mode) != baseline["mode"] \
                or hashlib.sha256(raw).hexdigest() != baseline["sha256"]:
            raise RollbackError(f"restored artifact {name} differs from baseline")
    if {path.name for path in restored_root.iterdir()} != expected_names:
        raise RollbackError("restored snapshot contains an unexpected entry")
    return manifest, manifest_raw


def atomic_receipt(path: Path, value: dict) -> bytes:
    parent = safe_directory(path.parent)
    if os.path.lexists(path):
        raise RollbackError("rollback receipt already exists")
    raw = canonical(value); descriptor, temporary = tempfile.mkstemp(prefix=".rollback-receipt.", dir=parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(raw); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path); fsync_directory(parent)
    finally:
        if os.path.lexists(temporary): os.unlink(temporary)
    return raw


def verify(backup: Path, restored: Path, plan_id: str, pending_marker: Path,
           output: Path, *, now: datetime | None = None) -> dict:
    if os.path.lexists(pending_marker):
        raise RollbackError("install pending marker still exists")
    _, manifest_raw = compare(backup, restored, plan_id)
    value = {"$schema": "v1-install-rollback-receipt.schema.json", "schema_version": 1,
             "status": "rollback_verified", "plan_id": plan_id,
             "verified_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
             "backup_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
             "restored_artifacts": len(REQUIRED) + len(OPTIONAL),
             "ordinary_oj_unchanged": True, "pending_marker_cleared": True}
    raw = atomic_receipt(output, value)
    return {"status": "rollback_verified", "plan_id": plan_id,
            "receipt_sha256": hashlib.sha256(raw).hexdigest(),
            "restored_artifacts": len(REQUIRED) + len(OPTIONAL)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-directory", required=True, type=Path)
    parser.add_argument("--restored-snapshot-directory", required=True, type=Path)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--pending-marker", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise RollbackError("install rollback verification requires Linux root")
        result = verify(args.backup_directory, args.restored_snapshot_directory,
                        args.plan_id, args.pending_marker, args.output)
        print(json.dumps(result, sort_keys=True)); return 0
    except (RollbackError, BackupError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
