#!/usr/bin/env python3
"""Quiesce the exact sealed controller before touching any dependency."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import sys

from apply_v1_controller import (ControllerPhaseError,Docker,backup_inputs,
    inspect_matches,live_files_match_backup,rollback as restore_controller_state,
    safe_docker_socket,wait_running)
from apply_v1_closed_frontend import Admin,adapt,caddy_canonical,get_live
from build_v1_cloud_install_backup import stable_health
from build_v1_ordinary_oj_install_backup import collect as collect_ordinary
from restore_v1_hydro_install_backup import live_matches_backup
from verify_v1_cloud_install_backup import validate as validate_cloud
from verify_v1_install_backup import BackupError,safe_file
from verify_v1_ordinary_oj_install_backup import compare as compare_ordinary,validate as validate_ordinary


HEX64=re.compile(r"^[a-f0-9]{64}$")


def cloud_matches(backup:Path)->None:
    try:
        baseline=validate_cloud(json.loads(safe_file(backup/"cloud-before.json")[0].decode("utf-8")))
        current=stable_health();observed={"schema_version":1,**{key:current.get(key) for key in baseline if key!="schema_version"}}
        if observed!=baseline:raise ControllerPhaseError("live cloud state differs before controller quiesce")
    except ControllerPhaseError:raise
    except Exception as exc:raise ControllerPhaseError("live cloud state could not be rebound before controller quiesce") from exc


def dependencies_match(args,backup:Path,manifest:dict,*,allow_hydro_restart:bool)->None:
    try:
        baseline_disk=safe_file(backup/"Caddyfile")[0];baseline_snippet=safe_file(backup/"caddy-exam.conf")[0]
        if safe_file(args.caddyfile)[0]!=baseline_disk or safe_file(args.snippet)[0]!=baseline_snippet:
            raise ControllerPhaseError("live Caddy disk state differs after controller quiesce")
        baseline_active=json.loads(safe_file(backup/"caddy-active.json")[0].decode("utf-8"));admin=Admin();live,_=get_live(admin)
        if caddy_canonical(live)!=caddy_canonical(baseline_active) or caddy_canonical(adapt(admin,baseline_disk))!=caddy_canonical(baseline_active):
            raise ControllerPhaseError("live Caddy active state differs after controller quiesce")
        if not live_matches_backup(backup,args.pm2_bin,manifest):
            raise ControllerPhaseError("live Hydro state differs after controller quiesce")
        baseline=validate_ordinary(json.loads(safe_file(backup/"ordinary-oj-before.json")[0].decode("utf-8")))
        current=validate_ordinary(collect_ordinary(args.oj_origin,args.pm2_bin))
        if allow_hydro_restart:compare_ordinary(baseline,current)
        elif current!=baseline:raise ControllerPhaseError("ordinary OJ changed during controller quiesce")
    except ControllerPhaseError:raise
    except Exception as exc:raise ControllerPhaseError("live dependency state could not be rebound after controller quiesce") from exc


def quiesce(args,docker:Docker,backup:Path,manifest:dict,baseline:dict,*,rollback:bool)->dict:
    if not baseline["present"] or baseline["container"]["running"] is not True:
        raise ControllerPhaseError("quiesce requires one running sealed controller baseline")
    if rollback:
        # This is also the recovery path for a late SQLite/config write that
        # was flushed while Docker was stopping the old process.  Restore the
        # sealed private files while keeping that exact process off.
        restore_controller_state(args,docker,backup,manifest,baseline,"")
    live=docker.inspect("noi-orchestrator",allow_absent=True)
    if not inspect_matches(live,baseline):raise ControllerPhaseError("live controller differs before quiesce")
    live_files_match_backup(args,backup,manifest)
    # Only the forward apply can obtain a fresh controller-reported cloud
    # snapshot.  Rollback runs after that controller has deliberately been
    # kept off; the terminal verifier checks cloud again after dependencies
    # are restored and this exact baseline controller is restarted.
    if not rollback:cloud_matches(backup)
    changed=False
    if (live.get("State") or {}).get("Running"):
        docker.stop(live["Id"]);wait_running(docker,"noi-orchestrator",False);changed=True
    stopped=docker.inspect("noi-orchestrator")
    if not inspect_matches(stopped,baseline) or (stopped.get("State") or {}).get("Running"):
        raise ControllerPhaseError("sealed controller did not remain exactly quiesced")
    # Bind the final state written by the controller before allowing Hydro or
    # Caddy to change.  A mismatch aborts while the controller is safely off.
    live_files_match_backup(args,backup,manifest)
    dependencies_match(args,backup,manifest,allow_hydro_restart=rollback)
    return {"status":"rollback_verified" if rollback else "verified","plan_id":args.plan_id,
            "backup_manifest_sha256":args.backup_manifest_sha256,"controller_id":stopped["Id"],
            "quiesced":True,"changed":changed,"other_container_mutations":0}


def main()->int:
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply",action="store_true");mode.add_argument("--rollback",action="store_true")
    parser.add_argument("--backup-directory",required=True,type=Path);parser.add_argument("--plan-id",required=True)
    parser.add_argument("--backup-manifest-sha256",required=True);parser.add_argument("--docker-socket",type=Path,default=Path("/var/run/docker.sock"))
    parser.add_argument("--project-config",required=True,type=Path);parser.add_argument("--project-env",required=True,type=Path)
    parser.add_argument("--database",required=True,type=Path);parser.add_argument("--caddyfile",required=True,type=Path)
    parser.add_argument("--snippet",required=True,type=Path);parser.add_argument("--pm2-bin",required=True,type=Path)
    parser.add_argument("--oj-origin",required=True);args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0 or not HEX64.fullmatch(args.plan_id) or not HEX64.fullmatch(args.backup_manifest_sha256):
            raise ControllerPhaseError("controller quiesce requires pinned Linux root")
        backup,manifest,baseline=backup_inputs(args.backup_directory,args.plan_id,args.backup_manifest_sha256)
        result=quiesce(args,Docker(safe_docker_socket(args.docker_socket)),backup,manifest,baseline,rollback=args.rollback)
        print(json.dumps(result,sort_keys=True));return 0
    except (ControllerPhaseError,BackupError,OSError,ValueError,json.JSONDecodeError,sqlite3.Error) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
