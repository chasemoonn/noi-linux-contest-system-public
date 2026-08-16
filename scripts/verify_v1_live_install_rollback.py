#!/usr/bin/env python3
"""Verify every live dependency, then restart the exact baseline controller."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys
import time

from apply_v1_controller import ControllerPhaseError, Docker, inspect_matches, safe_docker_socket
from apply_v1_closed_frontend import Admin, ClosedFrontendError, adapt, caddy_canonical, get_live
from commit_v1_caddy_config import CaddyCommitError
from build_v1_ordinary_oj_install_backup import collect as collect_ordinary
from collect_v1_ordinary_oj_observation import ObservationError
from restore_v1_hydro_install_backup import RestoreError, live_matches_backup
from verify_v1_cloud_install_backup import CloudBackupError, validate as validate_cloud
from verify_v1_controller_install_backup import ControllerBackupError, validate_definition
from verify_v1_install_backup import BackupError, safe_directory, safe_file, validate_manifest
from verify_v1_post_install import PostInstallError, contract, database_quiet, request, validate_health
from verify_v1_ordinary_oj_install_backup import compare as compare_ordinary,validate as validate_ordinary


HEX64=__import__("re").compile(r"^[a-f0-9]{64}$");RELEASE=__import__("re").compile(r"^[a-f0-9]{40}-[a-f0-9]{12}$")


class LiveRollbackError(RuntimeError):pass


def current_file(path:Path,backup:Path,mode:int)->None:
    raw,metadata=safe_file(path)
    expected,_=safe_file(backup)
    if raw!=expected or stat.S_IMODE(metadata.st_mode)!=mode:raise LiveRollbackError(f"restored live file differs: {path.name}")


def optional_file(path:Path,backup:Path,entry:dict)->None:
    if entry["present"]:current_file(path,backup,entry["mode"])
    elif os.path.lexists(path):raise LiveRollbackError(f"restored optional live file should be absent: {path.name}")


def pointer_matches(path:Path,backup:Path)->None:
    expected=safe_file(backup)[0].decode("utf-8").strip()
    if not __import__("re").fullmatch(r"source-releases/[a-f0-9]{40}-[a-f0-9]{12}",expected):
        raise LiveRollbackError("backup source pointer differs")
    metadata=os.lstat(path)
    if not stat.S_ISLNK(metadata.st_mode) or os.readlink(path)!=expected:raise LiveRollbackError("restored source pointer differs")


def wait_controller(docker:Docker,container_id:str,deadline=120)->dict:
    end=time.monotonic()+deadline
    while time.monotonic()<end:
        row=docker.inspect("noi-orchestrator",allow_absent=True)
        if row is not None and row.get("Id")==container_id and (row.get("State") or {}).get("Running"):
            try:
                _,raw=request("http://127.0.0.1:8600","/healthz")
                health=json.loads(raw.decode("utf-8"));validate_health(health);return row
            except (OSError,ValueError,PostInstallError,LiveRollbackError):pass
        time.sleep(1)
    raise LiveRollbackError("restored controller did not become exactly healthy")


def stop_failed_controller(docker:Docker,container_id:str,deadline=60)->None:
    row=docker.inspect("noi-orchestrator",allow_absent=True)
    if row is None or row.get("Id")!=container_id:
        raise LiveRollbackError("failed restored controller identity changed before fail-close")
    if not (row.get("State") or {}).get("Running"):return
    docker.stop(container_id);end=time.monotonic()+deadline
    while time.monotonic()<end:
        row=docker.inspect("noi-orchestrator",allow_absent=True)
        if row is not None and row.get("Id")==container_id and not (row.get("State") or {}).get("Running"):return
        time.sleep(1)
    raise LiveRollbackError("failed restored controller could not be stopped")


def verify(args)->dict:
    backup=safe_directory(args.backup_directory);manifest_raw,_=safe_file(backup/"backup-manifest.json",maximum=4*1024*1024)
    if hashlib.sha256(manifest_raw).hexdigest()!=args.backup_manifest_sha256:raise LiveRollbackError("backup manifest trust pin differs")
    manifest=json.loads(manifest_raw.decode("utf-8"));validate_manifest(manifest,backup,expected_plan_id=args.plan_id)
    expected=contract(args.expected_contract,args.plan_id,args.source_release);artifacts=manifest["artifacts"]
    pointer_matches(Path(expected["source_pointer"]),backup/"source-pointer.txt")
    current_file(Path(expected["project_config"]),backup/"orchestrator-config.yaml",artifacts["orchestrator_config"]["mode"])
    current_file(Path(expected["project_env"]),backup/"orchestrator.env",artifacts["orchestrator_env"]["mode"])
    database=Path(expected["database"]);current_file(database,backup/"orchestrator.db",artifacts["orchestrator_database"]["mode"])
    optional_file(Path(str(database)+"-wal"),backup/"orchestrator.db-wal",artifacts["orchestrator_database_wal"])
    optional_file(Path(str(database)+"-shm"),backup/"orchestrator.db-shm",artifacts["orchestrator_database_shm"])

    caddyfile=Path(expected["caddyfile"]);snippet=Path(expected["snippet"])
    current_file(caddyfile,backup/"Caddyfile",artifacts["caddyfile"]["mode"])
    current_file(snippet,backup/"caddy-exam.conf",artifacts["caddy_snippet"]["mode"])
    active=json.loads(safe_file(backup/"caddy-active.json")[0].decode("utf-8"));admin=Admin();live,_=get_live(admin)
    if caddy_canonical(live)!=caddy_canonical(active) or caddy_canonical(adapt(admin,safe_file(caddyfile)[0]))!=caddy_canonical(active):
        raise LiveRollbackError("restored Caddy disk/live state differs")
    if not live_matches_backup(backup,Path(expected["pm2_bin"]),manifest):raise LiveRollbackError("restored Hydro state differs")
    baseline=json.loads(safe_file(backup/"ordinary-oj-before.json")[0].decode("utf-8"));validate_ordinary(baseline)
    ordinary=collect_ordinary(expected["oj_origin"],Path(expected["pm2_bin"]));compare_ordinary(baseline,ordinary)

    baseline_cloud=validate_cloud(json.loads(safe_file(backup/"cloud-before.json")[0].decode("utf-8")))
    baseline_controller=validate_definition(json.loads(safe_file(backup/"controller-definition.json")[0].decode("utf-8")))
    if not baseline_controller["present"] or baseline_controller["container"]["running"] is not True:
        raise LiveRollbackError("baseline controller is not a running production controller")
    docker=Docker(safe_docker_socket(Path(expected["docker_socket"])));restored=docker.inspect("noi-orchestrator")
    if not inspect_matches(restored,baseline_controller):
        raise LiveRollbackError("baseline controller identity differs after dependency restoration")
    already_running=bool((restored.get("State") or {}).get("Running"))
    if not already_running:docker.start(restored["Id"])
    try:
        running=wait_controller(docker,restored["Id"])
        if not inspect_matches(running,baseline_controller):raise LiveRollbackError("started baseline controller identity differs")
        _,health_raw=request("http://127.0.0.1:8600","/healthz");health=json.loads(health_raw.decode("utf-8"));validate_health(health)
        observed={"schema_version":1,**{key:health["desktop_access"].get(key) for key in baseline_cloud if key!="schema_version"}}
        if observed!=baseline_cloud:raise LiveRollbackError("restored cloud state differs from baseline")
    except Exception as exc:
        # If this verifier started the controller, it owns fail-closing it.
        # A controller that was never quiesced (for example, source staging
        # failed before that phase) remains the sealed baseline process.
        if already_running:raise exc
        try:stop_failed_controller(docker,restored["Id"])
        except Exception as stop_exc:raise LiveRollbackError("rollback controller failed and could not be fail-closed") from stop_exc
        raise exc
    return {"status":"rollback_verified","plan_id":args.plan_id,"backup_manifest_sha256":args.backup_manifest_sha256}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--verify",action="store_true",required=True)
    parser.add_argument("--backup-directory",type=Path,required=True);parser.add_argument("--plan-id",required=True)
    parser.add_argument("--backup-manifest-sha256",required=True);parser.add_argument("--source-release",required=True)
    parser.add_argument("--expected-contract",type=Path,required=True);args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0 or not HEX64.fullmatch(args.plan_id) \
                or not HEX64.fullmatch(args.backup_manifest_sha256) or not RELEASE.fullmatch(args.source_release):
            raise LiveRollbackError("live rollback verification requires pinned Linux root")
        print(json.dumps(verify(args),sort_keys=True));return 0
    except (LiveRollbackError,ControllerPhaseError,ClosedFrontendError,CaddyCommitError,ObservationError,
            RestoreError,CloudBackupError,ControllerBackupError,BackupError,PostInstallError,
            OSError,ValueError,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
