#!/usr/bin/env python3
"""Verify the exact controller identity retained for V1 install rollback."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from verify_v1_install_backup import BackupError, safe_directory, safe_file, validate_manifest


HEX64 = re.compile(r"^[a-f0-9]{64}$")
IMAGE = re.compile(r"^sha256:[a-f0-9]{64}$")
NAME = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ControllerBackupError(ValueError): pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ControllerBackupError(f"{label} field set differs")
    return value


def validate_definition(value: dict) -> dict:
    row = exact(value, {"schema_version", "present", "container"}, "controller definition")
    if row["schema_version"] != 1 or not isinstance(row["present"], bool):
        raise ControllerBackupError("controller definition identity differs")
    if not row["present"]:
        if row["container"] is not None:
            raise ControllerBackupError("absent controller definition differs")
        return row
    container = exact(row["container"], {
        "container_id", "name", "image_id", "running", "restart_count",
        "immutable_identity", "immutable_identity_sha256",
    }, "controller container")
    if not HEX64.fullmatch(str(container["container_id"])) \
            or not NAME.fullmatch(str(container["name"])) \
            or not IMAGE.fullmatch(str(container["image_id"])) \
            or not isinstance(container["running"], bool) \
            or isinstance(container["restart_count"], bool) \
            or not isinstance(container["restart_count"], int) or container["restart_count"] < 0 \
            or not isinstance(container["immutable_identity"], dict) \
            or not HEX64.fullmatch(str(container["immutable_identity_sha256"])) \
            or hashlib.sha256(canonical(container["immutable_identity"])).hexdigest() \
            != container["immutable_identity_sha256"]:
        raise ControllerBackupError("controller container identity differs")
    if container["name"] != "/noi-orchestrator":
        raise ControllerBackupError("controller container name differs")
    return row


def validate_image(value: dict, definition: dict) -> dict:
    row = exact(value, {"schema_version", "present", "image_id"}, "controller image")
    if row["schema_version"] != 1 or row["present"] is not definition["present"]:
        raise ControllerBackupError("controller image presence differs")
    expected = definition["container"]["image_id"] if definition["present"] else None
    if row["image_id"] != expected or (expected is not None and not IMAGE.fullmatch(expected)):
        raise ControllerBackupError("controller image identity differs")
    return row


def verify(directory: Path, plan_id: str) -> dict:
    root = safe_directory(directory)
    manifest_raw, _ = safe_file(root / "backup-manifest.json", maximum=4 * 1024 * 1024)
    manifest = json.loads(manifest_raw.decode("utf-8"))
    validate_manifest(manifest, root, expected_plan_id=plan_id)
    definition = validate_definition(json.loads(safe_file(root / "controller-definition.json")[0].decode("utf-8")))
    image = validate_image(json.loads(safe_file(root / "controller-image.json")[0].decode("utf-8")), definition)
    return {"status": "passed", "plan_id": plan_id, "controller_present": definition["present"],
            "image_id": image["image_id"]}


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("backup_directory",type=Path)
    parser.add_argument("--expected-plan-id",required=True); args=parser.parse_args()
    try:
        print(json.dumps(verify(args.backup_directory,args.expected_plan_id),sort_keys=True)); return 0
    except (ControllerBackupError,BackupError,OSError,UnicodeDecodeError,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr); return 2


if __name__=="__main__": raise SystemExit(main())
