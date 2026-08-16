#!/usr/bin/env python3
"""Durably seal an already collected exact V1 install backup directory."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tempfile

from verify_v1_install_backup import (
    BackupError, HEX40, HEX64, OPTIONAL, REQUIRED, safe_directory, safe_file,
    validate_manifest,
)
from verify_v1_hydro_install_backup import verify as verify_hydro_backup, verify_pm2, verify_tree_archive
from verify_v1_controller_install_backup import validate_definition, validate_image
from verify_v1_cloud_install_backup import validate as validate_cloud
from verify_v1_ordinary_oj_install_backup import validate as validate_ordinary


class BuildError(RuntimeError):
    pass


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def seal_file(path: Path) -> tuple[bytes, os.stat_result]:
    raw, metadata = safe_file(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise BuildError("backup artifact changed before fsync")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return raw, metadata


def build(directory: Path, plan_id: str, revision: str, manifest_sha256: str,
          *, now: datetime | None = None) -> dict:
    root = safe_directory(directory)
    if not HEX64.fullmatch(plan_id) or not HEX40.fullmatch(revision) or not HEX64.fullmatch(manifest_sha256):
        raise BuildError("backup identity is invalid")
    manifest_path = root / "backup-manifest.json"
    if os.path.lexists(manifest_path):
        raise BuildError("backup manifest already exists")
    allowed = set(REQUIRED.values()) | set(OPTIONAL.values())
    observed = {path.name for path in root.iterdir()}
    unknown = observed - allowed
    if unknown:
        raise BuildError("backup directory contains an unmanifested entry")
    artifacts = {}
    for name, filename in {**REQUIRED, **OPTIONAL}.items():
        path = root / filename; present = os.path.lexists(path)
        if name in REQUIRED and not present:
            raise BuildError(f"required backup artifact {name} is absent")
        if not present:
            artifacts[name] = {"filename": filename, "present": False,
                               "bytes": None, "mode": None, "sha256": None}
            continue
        raw, metadata = seal_file(path)
        artifacts[name] = {"filename": filename, "present": True,
                           "bytes": len(raw), "mode": stat.S_IMODE(metadata.st_mode),
                           "sha256": hashlib.sha256(raw).hexdigest()}
    # Reject semantically ambiguous Hydro backups before publishing the
    # terminal backup manifest.  The complete verifier runs again afterward.
    addon_raw, _ = safe_file(root / REQUIRED["hydro_addon_tree"])
    state_raw, _ = safe_file(root / REQUIRED["hydro_plugin_state"])
    dump_raw, _ = safe_file(root / REQUIRED["pm2_dump"])
    definition_raw, _ = safe_file(root / REQUIRED["hydro_pm2_definition"])
    verify_tree_archive(addon_raw, REQUIRED["hydro_addon_tree"])
    verify_tree_archive(state_raw, REQUIRED["hydro_plugin_state"])
    verify_pm2(dump_raw, definition_raw)
    controller_definition = validate_definition(json.loads(
        safe_file(root / REQUIRED["controller_definition"])[0].decode("utf-8")
    ))
    validate_image(json.loads(
        safe_file(root / REQUIRED["controller_image"])[0].decode("utf-8")
    ), controller_definition)
    if not controller_definition["present"] or controller_definition["container"]["running"] is not True:
        raise BuildError("production upgrade requires one running controller baseline")
    validate_cloud(json.loads(safe_file(root / REQUIRED["cloud_snapshot"])[0].decode("utf-8")))
    validate_ordinary(json.loads(safe_file(root / REQUIRED["ordinary_oj_snapshot"])[0].decode("utf-8")))
    fsync_directory(root)
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    value = {"$schema": "v1-install-backup-manifest.schema.json", "schema_version": 1,
             "plan_id": plan_id, "source": {"revision": revision,
             "manifest_sha256": manifest_sha256},
             "created_at": created.isoformat().replace("+00:00", "Z"), "artifacts": artifacts}
    descriptor, temporary = tempfile.mkstemp(prefix=".backup-manifest.", dir=root)
    try:
        if hasattr(os, "fchmod"): os.fchmod(descriptor, 0o600)
        else: os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            output.write(canonical(value)); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, manifest_path); fsync_directory(root)
    finally:
        if descriptor >= 0: os.close(descriptor)
        if os.path.lexists(temporary): os.unlink(temporary)
    validate_manifest(value, root, expected_plan_id=plan_id)
    verify_hydro_backup(root, plan_id)
    from verify_v1_controller_install_backup import verify as verify_controller_backup
    verify_controller_backup(root, plan_id)
    from verify_v1_cloud_install_backup import verify as verify_cloud_backup
    verify_cloud_backup(root, plan_id)
    from verify_v1_ordinary_oj_install_backup import verify as verify_ordinary_backup
    verify_ordinary_backup(root, plan_id)
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup_directory", type=Path)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise BuildError("install backup sealing requires Linux root")
        value = build(args.backup_directory, args.plan_id, args.source_revision,
                      args.expected_manifest_sha256)
        raw, _ = safe_file(args.backup_directory / "backup-manifest.json")
        print(json.dumps({"status": "sealed", "plan_id": value["plan_id"],
                          "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                          "artifacts": len(value["artifacts"])}, sort_keys=True)); return 0
    except (BuildError, BackupError, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
