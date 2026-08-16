#!/usr/bin/env python3
"""Validate the privacy-safe, fail-closed cloud baseline for V1 upgrade."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from verify_v1_install_backup import BackupError, safe_directory, safe_file, validate_manifest


class CloudBackupError(ValueError):pass


def validate(value:dict)->dict:
    keys={"schema_version","enabled","desired_open","open","closed","healthy","managed_count","conflict_count",
          "management_healthy","management_missing_count","instance_state"}
    if not isinstance(value,dict) or set(value)!=keys or value["schema_version"]!=1:
        raise CloudBackupError("cloud baseline field set differs")
    booleans={"enabled":True,"desired_open":False,"open":False,"closed":True,"healthy":True,"management_healthy":True}
    if any(value[name] is not expected for name,expected in booleans.items()) \
            or value["managed_count"]!=0 or value["conflict_count"]!=0 \
            or value["management_missing_count"]!=0 or value["instance_state"]!="STOPPED":
        raise CloudBackupError("cloud baseline is not exactly closed")
    return value


def verify(directory:Path,plan_id:str)->dict:
    root=safe_directory(directory);manifest_raw,_=safe_file(root/"backup-manifest.json",maximum=4*1024*1024)
    manifest=json.loads(manifest_raw.decode("utf-8"));validate_manifest(manifest,root,expected_plan_id=plan_id)
    value=validate(json.loads(safe_file(root/"cloud-before.json",maximum=1024*1024)[0].decode("utf-8")))
    return {"status":"passed","plan_id":plan_id,"closed":value["closed"],"instance_state":value["instance_state"]}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("backup_directory",type=Path);parser.add_argument("--expected-plan-id",required=True);args=parser.parse_args()
    try:print(json.dumps(verify(args.backup_directory,args.expected_plan_id),sort_keys=True));return 0
    except (CloudBackupError,BackupError,OSError,UnicodeDecodeError,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
