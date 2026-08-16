#!/usr/bin/env python3
"""Validate and compare the ordinary-OJ install isolation baseline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from verify_v1_install_backup import BackupError,safe_directory,safe_file,validate_manifest


HEX64=re.compile(r"^[a-f0-9]{64}$");NAMES=("caddy","hydro-sandbox","hydrooj","mongodb")


class OrdinaryBackupError(ValueError):pass


def validate(value:dict)->dict:
    keys={"schema_version","homepage_status","login_status","prep_health_ok","prep_database_ok","processes"}
    if not isinstance(value,dict) or set(value)!=keys or value["schema_version"]!=1 \
            or value["homepage_status"]!=200 or value["login_status"]!=200 \
            or value["prep_health_ok"] is not True or value["prep_database_ok"] is not True:
        raise OrdinaryBackupError("ordinary OJ install baseline differs")
    rows=value["processes"]
    if not isinstance(rows,list) or len(rows)!=4:raise OrdinaryBackupError("ordinary OJ PM2 baseline differs")
    observed=[]
    for row in rows:
        if not isinstance(row,dict) or set(row)!={"name","pid","restart_time","status"} or row["name"] not in NAMES \
                or row["status"]!="online" or isinstance(row["pid"],bool) or not isinstance(row["pid"],int) or row["pid"]<=0 \
                or isinstance(row["restart_time"],bool) or not isinstance(row["restart_time"],int) or row["restart_time"]<0:
            raise OrdinaryBackupError("ordinary OJ PM2 baseline differs")
        observed.append(row["name"])
    if tuple(observed)!=NAMES:raise OrdinaryBackupError("ordinary OJ PM2 order differs")
    return value


def compare(baseline:dict,current:dict)->None:
    first=validate(baseline);second=validate(current);stable={"caddy","hydro-sandbox","mongodb"}
    before={row["name"]:row for row in first["processes"]};after={row["name"]:row for row in second["processes"]}
    if any(after[name]!=before[name] for name in stable):raise OrdinaryBackupError("ordinary OJ stable PM2 process changed")


def verify(directory:Path,plan_id:str)->dict:
    root=safe_directory(directory);manifest_raw,_=safe_file(root/"backup-manifest.json",maximum=4*1024*1024)
    manifest=json.loads(manifest_raw.decode("utf-8"));validate_manifest(manifest,root,expected_plan_id=plan_id)
    validate(json.loads(safe_file(root/"ordinary-oj-before.json",maximum=1024*1024)[0].decode("utf-8")))
    return {"status":"passed","plan_id":plan_id,"stable_processes":3,"hydro_restart_allowed":True}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("backup_directory",type=Path);parser.add_argument("--expected-plan-id",required=True);args=parser.parse_args()
    try:print(json.dumps(verify(args.backup_directory,args.expected_plan_id),sort_keys=True));return 0
    except (OrdinaryBackupError,BackupError,OSError,UnicodeDecodeError,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
