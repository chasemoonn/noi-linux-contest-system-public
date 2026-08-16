#!/usr/bin/env python3
"""Verify a sealed baseline captured before the first NOI installation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys

from verify_v1_install_backup import BackupError, safe_directory, safe_file, validate_manifest
from verify_v1_hydro_install_backup import verify as verify_hydro
from verify_v1_controller_install_backup import verify as verify_controller
from verify_v1_cloud_install_backup import verify as verify_cloud
from verify_v1_ordinary_oj_install_backup import verify as verify_ordinary


PATH_KEYS = {
    "install_root", "source_pointer", "project_config", "project_env", "database",
    "database_wal", "database_shm", "caddy_snippet", "hydro_addon_tree",
    "hydro_plugin_env", "hydro_plugin_token", "hydro_plugin_state",
}


class CleanBackupError(ValueError):
    pass


def validate_clean_target(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema_version", "operation", "paths"} \
            or value["schema_version"] != 1 or value["operation"] != "clean-install" \
            or not isinstance(value["paths"], dict) or set(value["paths"]) != PATH_KEYS:
        raise CleanBackupError("clean target identity differs")
    observed = []
    for key, row in value["paths"].items():
        if not isinstance(row, dict) or set(row) != {"path", "present"} \
                or row["present"] is not False or not isinstance(row["path"], str):
            raise CleanBackupError(f"clean target path differs: {key}")
        path = PurePosixPath(row["path"])
        if not path.is_absolute() or ".." in path.parts or row["path"] in observed:
            raise CleanBackupError(f"clean target path identity differs: {key}")
        observed.append(row["path"])
    return value


def verify(directory: Path, plan_id: str) -> dict:
    root = safe_directory(directory)
    manifest_raw, _ = safe_file(root / "backup-manifest.json", maximum=4 * 1024 * 1024)
    manifest = validate_manifest(json.loads(manifest_raw.decode("utf-8")), root,
                                 expected_plan_id=plan_id)
    if manifest.get("operation") != "clean-install":
        raise CleanBackupError("backup is not a clean install baseline")
    target = validate_clean_target(json.loads(
        safe_file(root / "clean-target.json", maximum=1024 * 1024)[0].decode("utf-8")
    ))
    for key, row in target["paths"].items():
        if os.path.lexists(row["path"]):
            raise CleanBackupError(f"clean target path appeared after capture: {key}")
    hydro = verify_hydro(root, plan_id)
    if hydro["addon_present"] is not False or hydro["state_present"] is not False:
        raise CleanBackupError("clean target contains an NOI Hydro tree")
    controller = verify_controller(root, plan_id)
    if controller["controller_present"] is not False:
        raise CleanBackupError("clean target contains an NOI controller")
    verify_cloud(root, plan_id); verify_ordinary(root, plan_id)
    return {"status": "passed", "operation": "clean-install", "plan_id": plan_id,
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "clean_paths": len(target["paths"]), "controller_present": False,
            "addon_present": False, "state_present": False}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("backup_directory", type=Path)
    parser.add_argument("--expected-plan-id", required=True); args = parser.parse_args()
    try:
        print(json.dumps(verify(args.backup_directory, args.expected_plan_id), sort_keys=True)); return 0
    except (BackupError, CleanBackupError, OSError, ValueError, UnicodeDecodeError,
            json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
