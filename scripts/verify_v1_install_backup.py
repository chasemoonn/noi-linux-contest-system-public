#!/usr/bin/env python3
"""Verify the exact durable backup set required before service mutation."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys


HEX40 = re.compile(r"^[a-f0-9]{40}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
REQUIRED = {
    "source_pointer": "source-pointer.txt",
    "orchestrator_config": "orchestrator-config.yaml",
    "orchestrator_env": "orchestrator.env",
    "orchestrator_database": "orchestrator.db",
    "controller_definition": "controller-definition.json",
    "controller_image": "controller-image.json",
    "caddyfile": "Caddyfile",
    "caddy_active": "caddy-active.json",
    "caddy_snippet": "caddy-exam.conf",
    "hydro_addon_json": "hydro-addon.json",
    "hydro_addon_tree": "hydro-addon-tree.tar",
    "hydro_plugin_env": "hydro-plugin.env",
    "hydro_plugin_token": "hydro-plugin-token",
    "hydro_plugin_state": "hydro-plugin-state.tar",
    "pm2_dump": "pm2-dump.json",
    "hydro_pm2_definition": "hydro-pm2-definition.json",
    "ordinary_oj_snapshot": "ordinary-oj-before.json",
    "cloud_snapshot": "cloud-before.json",
}
OPTIONAL = {
    "orchestrator_database_wal": "orchestrator.db-wal",
    "orchestrator_database_shm": "orchestrator.db-shm",
    "pm2_dump_backup": "pm2-dump.backup.json",
}
CLEAN_ONLY = {
    "clean_target": "clean-target.json",
}
CLEAN_REQUIRED = {
    "controller_definition", "controller_image", "caddyfile", "caddy_active",
    "hydro_addon_json", "hydro_addon_tree", "hydro_plugin_state", "pm2_dump",
    "hydro_pm2_definition", "ordinary_oj_snapshot", "cloud_snapshot", "clean_target",
}
CLEAN_MUST_BE_ABSENT = {
    "source_pointer", "orchestrator_config", "orchestrator_env",
    "orchestrator_database", "orchestrator_database_wal", "orchestrator_database_shm",
    "caddy_snippet", "hydro_plugin_env", "hydro_plugin_token",
}


class BackupError(ValueError):
    pass


def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise BackupError(f"{label} field set differs")
    return value


def safe_directory(path: Path) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    metadata = os.lstat(requested)
    if requested != resolved or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BackupError("backup directory is unsafe")
    if platform.system().lower() == "linux" and (
        metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BackupError("backup directory must be root-owned mode 0700")
    return resolved


def safe_file(path: Path, maximum: int = 512 * 1024 * 1024) -> tuple[bytes, os.stat_result]:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 \
            or metadata.st_size < 0 or metadata.st_size > maximum:
        raise BackupError("backup artifact metadata is unsafe")
    if platform.system().lower() == "linux" and metadata.st_uid != 0:
        raise BackupError("backup artifact is not root-owned")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor); raw = os.read(descriptor, opened.st_size + 1)
        if len(raw) != opened.st_size or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise BackupError("backup artifact changed while reading")
        return raw, opened
    finally:
        os.close(descriptor)


def validate_manifest(value: dict, directory: Path, *, expected_plan_id: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise BackupError("backup manifest field set differs")
    clean = value.get("$schema") == "v1-clean-install-backup-manifest.schema.json"
    keys = {"$schema", "schema_version", "plan_id", "source", "created_at", "artifacts"}
    if clean:
        keys.add("operation")
    row = exact(value, keys, "backup manifest")
    if row["$schema"] not in {"v1-install-backup-manifest.schema.json",
                              "v1-clean-install-backup-manifest.schema.json"} \
            or row["schema_version"] != 1 or not HEX64.fullmatch(str(row["plan_id"])):
        raise BackupError("backup manifest identity differs")
    if clean and row["operation"] != "clean-install":
        raise BackupError("clean install backup operation differs")
    if expected_plan_id is not None and row["plan_id"] != expected_plan_id:
        raise BackupError("backup manifest plan ID differs")
    source = exact(row["source"], {"revision", "manifest_sha256"}, "backup source")
    if not HEX40.fullmatch(str(source["revision"])) or not HEX64.fullmatch(str(source["manifest_sha256"])):
        raise BackupError("backup source identity differs")
    try:
        observed_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("backup timestamp is invalid") from exc
    if observed_at.tzinfo is None:
        raise BackupError("backup timestamp has no timezone")
    definitions = {**REQUIRED, **OPTIONAL, **(CLEAN_ONLY if clean else {})}
    artifacts = exact(row["artifacts"], set(definitions), "backup artifacts")
    expected_names = {"backup-manifest.json"}
    required_present = CLEAN_REQUIRED if clean else set(REQUIRED)
    for name, filename in definitions.items():
        entry = exact(artifacts[name], {"filename", "present", "bytes", "mode", "sha256"}, f"backup artifact {name}")
        if entry["filename"] != filename or not isinstance(entry["present"], bool):
            raise BackupError(f"backup artifact {name} identity differs")
        if name in required_present and entry["present"] is not True:
            raise BackupError(f"required backup artifact {name} is absent")
        if clean and name in CLEAN_MUST_BE_ABSENT and entry["present"] is not False:
            raise BackupError(f"clean target artifact {name} must be absent")
        path = directory / filename
        if not entry["present"]:
            if any(entry[key] is not None for key in ("bytes", "mode", "sha256")) or os.path.lexists(path):
                raise BackupError(f"absent backup artifact {name} differs")
            continue
        if not isinstance(entry["bytes"], int) or entry["bytes"] < 0 or not isinstance(entry["mode"], int) \
                or entry["mode"] < 0 or entry["mode"] > 0o777 or not HEX64.fullmatch(str(entry["sha256"])):
            raise BackupError(f"backup artifact {name} metadata differs")
        try:
            raw, metadata = safe_file(path)
        except FileNotFoundError as exc:
            raise BackupError(f"backup artifact {name} is missing") from exc
        if len(raw) != entry["bytes"] or stat.S_IMODE(metadata.st_mode) != entry["mode"] \
                or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise BackupError(f"backup artifact {name} bytes differ")
        expected_names.add(filename)
    observed_names = {path.name for path in directory.iterdir()}
    if observed_names != expected_names:
        raise BackupError("backup directory contains an unmanifested entry")
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup_directory", type=Path)
    parser.add_argument("--expected-plan-id")
    args = parser.parse_args()
    try:
        directory = safe_directory(args.backup_directory)
        manifest_raw, _ = safe_file(directory / "backup-manifest.json", maximum=4 * 1024 * 1024)
        value = json.loads(manifest_raw.decode("utf-8"))
        validate_manifest(value, directory, expected_plan_id=args.expected_plan_id)
        # Import only after this module is fully initialized; the semantic
        # verifier reuses the safe file and manifest primitives above.
        from verify_v1_hydro_install_backup import verify as verify_hydro_backup
        verify_hydro_backup(directory, value["plan_id"])
        from verify_v1_controller_install_backup import verify as verify_controller_backup
        controller=verify_controller_backup(directory, value["plan_id"])
        if controller["controller_present"] is not True:
            raise BackupError("production upgrade requires one running controller baseline")
        definition=json.loads(safe_file(directory/REQUIRED["controller_definition"])[0].decode("utf-8"))
        if definition["container"]["running"] is not True:
            raise BackupError("production upgrade requires one running controller baseline")
        from verify_v1_cloud_install_backup import verify as verify_cloud_backup
        verify_cloud_backup(directory, value["plan_id"])
        from verify_v1_ordinary_oj_install_backup import verify as verify_ordinary_backup
        verify_ordinary_backup(directory, value["plan_id"])
        print(json.dumps({"status": "passed", "plan_id": value["plan_id"],
                          "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                          "artifacts": len(value["artifacts"])}, sort_keys=True)); return 0
    except (BackupError, OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
