#!/usr/bin/env python3
"""Verify the semantic Hydro/PM2 subset of a sealed V1 install backup."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile

from verify_v1_install_backup import BackupError, safe_directory, safe_file, validate_manifest


HEX64 = re.compile(r"^[a-f0-9]{64}$")
TREE_ROOTS = {
    "hydro-addon-tree.tar": "/root/.hydro/addons/orchestrator-submit",
    "hydro-plugin-state.tar": "/root/.hydro/orchestrator-state",
}
LAUNCH_KEYS = (
    "name", "pm_exec_path", "pm_cwd", "exec_interpreter", "args", "node_args",
    "exec_mode", "instances", "autorestart", "restart_delay", "max_restarts",
    "min_uptime", "kill_timeout", "listen_timeout", "wait_ready",
    "max_memory_restart", "pm_out_log_path", "pm_err_log_path", "merge_logs", "namespace",
)


def normalized_launch(row: dict) -> dict:
    """Return the restorable PM2 launch contract.

    PM2 6 serializes a single fork-mode process without an ``instances``
    field in ``dump.pm2``, while the corresponding live ``pm2_env`` reports
    ``instances=1``.  Those representations are operationally identical and
    round-trip through ``pm2 resurrect``.  Keep every other field exact and
    do not extend this equivalence to cluster mode.
    """
    result = {key: row.get(key) for key in LAUNCH_KEYS}
    if result["exec_mode"] == "fork_mode" and result["instances"] is None:
        result["instances"] = 1
    return result


class HydroBackupError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise HydroBackupError("tree entry path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise HydroBackupError("tree entry path is unsafe")
    return path.as_posix()


def verify_tree_archive(raw: bytes, filename: str) -> dict:
    expected_root = TREE_ROOTS[filename]
    try:
        bundle = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except tarfile.TarError as exc:
        raise HydroBackupError(f"{filename} is not a plain tar archive") from exc
    with bundle:
        members = bundle.getmembers()
        names = [member.name for member in members]
        if names.count("tree-state.json") != 1 or len(names) != len(set(names)):
            raise HydroBackupError(f"{filename} state manifest differs")
        state_member = members[names.index("tree-state.json")]
        if not state_member.isfile() or state_member.issym() or state_member.islnk() \
                or state_member.mode & 0o777 != 0o600 or state_member.size > 4 * 1024 * 1024:
            raise HydroBackupError(f"{filename} state manifest metadata differs")
        handle = bundle.extractfile(state_member)
        try: state = json.loads((handle.read() if handle else b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HydroBackupError(f"{filename} state manifest is invalid") from exc
        if not isinstance(state, dict) or set(state) != {"schema_version", "root", "present", "root_mode", "entries"} \
                or state["schema_version"] != 1 or state["root"] != expected_root \
                or not isinstance(state["present"], bool) or not isinstance(state["entries"], list):
            raise HydroBackupError(f"{filename} state identity differs")
        if (state["present"] and (not isinstance(state["root_mode"], int)
                or not 0 <= state["root_mode"] <= 0o777)) or (
                not state["present"] and state["root_mode"] is not None):
            raise HydroBackupError(f"{filename} root mode differs")
        if not state["present"] and (state["entries"] or len(members) != 1):
            raise HydroBackupError(f"{filename} absent tree contains payload")
        expected_payload = set()
        seen = set()
        declared_directories = set()
        ordered_paths = []
        for entry in state["entries"]:
            fields = {"path", "type", "mode", "bytes", "sha256"}
            if not isinstance(entry, dict) or set(entry) != fields:
                raise HydroBackupError(f"{filename} tree entry fields differ")
            relative = _relative(entry["path"])
            if relative in seen or entry["type"] not in {"directory", "file"} \
                    or not isinstance(entry["mode"], int) or not 0 <= entry["mode"] <= 0o777:
                raise HydroBackupError(f"{filename} tree entry identity differs")
            seen.add(relative)
            ordered_paths.append(relative)
            if entry["type"] == "directory":
                if entry["bytes"] is not None or entry["sha256"] is not None:
                    raise HydroBackupError(f"{filename} directory metadata differs")
                declared_directories.add(relative); continue
            if not isinstance(entry["bytes"], int) or not 0 <= entry["bytes"] <= 256 * 1024 * 1024 \
                    or not HEX64.fullmatch(str(entry["sha256"])):
                raise HydroBackupError(f"{filename} file metadata differs")
            payload = f"tree/{relative}"; expected_payload.add(payload)
            if payload not in names:
                raise HydroBackupError(f"{filename} file payload is missing")
            member = members[names.index(payload)]
            if not member.isfile() or member.issym() or member.islnk() or member.mode & 0o777 != entry["mode"] \
                    or member.size != entry["bytes"]:
                raise HydroBackupError(f"{filename} file payload metadata differs")
            handle = bundle.extractfile(member); content = handle.read() if handle else b""
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise HydroBackupError(f"{filename} file payload hash differs")
        if ordered_paths != sorted(ordered_paths):
            raise HydroBackupError(f"{filename} tree entries are not canonical")
        for relative in seen:
            parent = PurePosixPath(relative).parent
            while parent != PurePosixPath("."):
                if parent.as_posix() not in declared_directories:
                    raise HydroBackupError(f"{filename} tree entry parent is missing")
                parent = parent.parent
        if set(names) != {"tree-state.json"} | expected_payload:
            raise HydroBackupError(f"{filename} contains an unexpected payload")
        return state


def verify_pm2(dump_raw: bytes, definition_raw: bytes) -> dict:
    try:
        dump = json.loads(dump_raw.decode("utf-8")); definition = json.loads(definition_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HydroBackupError("PM2 baseline JSON is invalid") from exc
    fields = {"schema_version", "name", "dump_row_sha256", "normalized_env_sha256",
              "orchestrator_prefix_sha256", "top_orchestrator_prefix_sha256", "launch"}
    if not isinstance(definition, dict) or set(definition) != fields or definition["schema_version"] != 1 \
            or definition["name"] != "hydrooj" or not isinstance(dump, list):
        raise HydroBackupError("PM2 Hydro definition identity differs")
    matches = [row for row in dump if isinstance(row, dict) and row.get("name") == "hydrooj"]
    if len(matches) != 1:
        raise HydroBackupError("PM2 dump must contain exactly one Hydro definition")
    row = matches[0]; environment = row.get("env")
    if not isinstance(environment, dict) or any(not isinstance(key, str) for key in environment):
        raise HydroBackupError("PM2 Hydro environment differs")
    hashes = (
        definition["dump_row_sha256"], definition["normalized_env_sha256"],
        definition["orchestrator_prefix_sha256"], definition["top_orchestrator_prefix_sha256"],
    )
    if any(not HEX64.fullmatch(str(value)) for value in hashes):
        raise HydroBackupError("PM2 Hydro definition hash differs")
    normalized = dict(environment); normalized.pop("unique_id", None)
    prefix = {key: value for key, value in environment.items() if key.startswith("ORCHESTRATOR_")}
    top = {key: value for key, value in row.items() if key.startswith("ORCHESTRATOR_")}
    if hashlib.sha256(canonical(row)).hexdigest() != definition["dump_row_sha256"] \
            or hashlib.sha256(canonical(normalized)).hexdigest() != definition["normalized_env_sha256"] \
            or hashlib.sha256(canonical(prefix)).hexdigest() != definition["orchestrator_prefix_sha256"] \
            or hashlib.sha256(canonical(top)).hexdigest() != definition["top_orchestrator_prefix_sha256"]:
        raise HydroBackupError("PM2 Hydro definition does not match the dump")
    launch = definition["launch"]
    if not isinstance(launch, dict) or launch != normalized_launch(row):
        raise HydroBackupError("PM2 Hydro launch definition differs")
    return definition


def verify(directory: Path, plan_id: str) -> dict:
    root = safe_directory(directory)
    manifest_raw, _ = safe_file(root / "backup-manifest.json", maximum=4 * 1024 * 1024)
    try: manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HydroBackupError("backup manifest is invalid") from exc
    validate_manifest(manifest, root, expected_plan_id=plan_id)
    addon_raw, _ = safe_file(root / "hydro-addon-tree.tar")
    state_raw, _ = safe_file(root / "hydro-plugin-state.tar")
    dump_raw, _ = safe_file(root / "pm2-dump.json")
    definition_raw, _ = safe_file(root / "hydro-pm2-definition.json")
    addon = verify_tree_archive(addon_raw, "hydro-addon-tree.tar")
    state = verify_tree_archive(state_raw, "hydro-plugin-state.tar")
    pm2 = verify_pm2(dump_raw, definition_raw)
    return {"status": "passed", "plan_id": plan_id, "addon_present": addon["present"],
            "state_present": state["present"], "pm2_name": pm2["name"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup_directory", type=Path)
    parser.add_argument("--expected-plan-id", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.backup_directory, args.expected_plan_id)
        print(json.dumps(result, sort_keys=True)); return 0
    except (HydroBackupError, BackupError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
