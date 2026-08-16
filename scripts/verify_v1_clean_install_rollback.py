#!/usr/bin/env python3
"""Read-only terminal proof that a failed first install returned to no NOI install."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys

from apply_v1_closed_frontend import Admin,adapt,caddy_canonical,get_live
from apply_v1_controller import Docker,safe_docker_socket,safe_private_file
from build_v1_ordinary_oj_install_backup import collect as collect_ordinary
from collect_v1_ordinary_oj_observation import ObservationError
from restore_v1_hydro_install_backup import RestoreError,live_matches_backup
from verify_v1_clean_install_backup import validate_clean_target
from verify_v1_cloud_install_backup import validate as validate_cloud
from verify_v1_install_backup import BackupError,safe_directory,safe_file,validate_manifest
from verify_v1_ordinary_oj_install_backup import compare as compare_ordinary,validate as validate_ordinary
from verify_v1_post_install import PostInstallError,contract


HEX64=re.compile(r"^[a-f0-9]{64}$");RELEASE=re.compile(r"^[a-f0-9]{40}-[a-f0-9]{12}$")


class CleanRollbackError(RuntimeError):pass


def absent(path:Path,label:str)->None:
    if os.path.lexists(path):raise CleanRollbackError(f"clean rollback target is still present: {label}")


def closed_cloud_snapshot(value:dict)->dict:
    """Bind provider actual state to the rollback's explicit closed intent.

    ``desktop_access_status`` is deliberately an actual-state API and does not
    expose the controller's ``desired_open`` field.  During a clean rollback
    the desired state is nevertheless fixed by this verifier: no contest or
    controller exists, so the only admissible intent is closed.
    """
    keys=("enabled","open","closed","healthy","managed_count","conflict_count",
          "management_healthy","management_missing_count","instance_state")
    return validate_cloud({"schema_version":1,"desired_open":False,
                           **{key:value.get(key) for key in keys}})


def cloud_snapshot(release:Path,definition_path:Path,config_path:Path)->dict:
    raw,_=safe_private_file(definition_path,"desired controller definition",maximum=2*1024*1024)
    try:definition=json.loads(raw.decode())
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise CleanRollbackError("desired controller definition is invalid") from exc
    rows=(definition.get("config") or {}).get("Env")
    if not isinstance(rows,list):raise CleanRollbackError("desired controller environment differs")
    environment={}
    for row in rows:
        if not isinstance(row,str) or "=" not in row:raise CleanRollbackError("desired controller environment row differs")
        key,value=row.split("=",1)
        if key in environment or not re.fullmatch(r"[A-Z_][A-Z0-9_]*",key):raise CleanRollbackError("desired controller environment keys differ")
        environment[key]=value
    previous=os.environ.copy();old_path=list(sys.path)
    try:
        os.environ.clear();os.environ.update(environment)
        sys.path.insert(0,str(release/"orchestrator"))
        # Never accept a process-wide cached `services` module.  The rollback
        # proof must execute the cloud reader from the exact frozen release.
        for name in tuple(sys.modules):
            if name=="services" or name.startswith("services."):
                del sys.modules[name]
        config_module=importlib.import_module("services.config")
        cloud_module=importlib.import_module("services.cloud")
        services_root=(release/"orchestrator/services").resolve(strict=True)
        for module,label in ((config_module,"config"),(cloud_module,"cloud")):
            origin=Path(module.__file__).resolve(strict=True)
            if origin.parent!=services_root:
                raise CleanRollbackError(f"frozen {label} module origin differs")
        cfg=config_module.load_config(config_path);provider=cloud_module.make_cvm(cfg["cloud"])
        value=provider.desktop_access_status()
    except Exception as exc:raise CleanRollbackError("read-only cloud rollback probe failed") from exc
    finally:
        os.environ.clear();os.environ.update(previous);sys.path[:]=old_path
    return closed_cloud_snapshot(value)


def verify(args)->dict:
    backup=safe_directory(args.backup_directory);manifest_raw,_=safe_file(backup/"backup-manifest.json",maximum=4*1024*1024)
    if hashlib.sha256(manifest_raw).hexdigest()!=args.backup_manifest_sha256:raise CleanRollbackError("clean backup trust pin differs")
    manifest=validate_manifest(json.loads(manifest_raw.decode()),backup,expected_plan_id=args.plan_id)
    if manifest.get("operation")!="clean-install":raise CleanRollbackError("rollback baseline is not clean")
    target=validate_clean_target(json.loads(safe_file(backup/"clean-target.json")[0].decode()))
    expected=contract(args.expected_contract,args.plan_id,args.source_release)
    root=Path(target["paths"]["install_root"]["path"])
    if Path(expected["source_pointer"])!=Path(target["paths"]["source_pointer"]["path"]):
        raise CleanRollbackError("clean source pointer binding differs")
    absent(Path(expected["source_pointer"]),"source pointer")
    for key in ("project_config","project_env","database","database_wal","database_shm","caddy_snippet",
                "hydro_addon_tree","hydro_plugin_env","hydro_plugin_token","hydro_plugin_state"):
        absent(Path(target["paths"][key]["path"]),key)

    caddyfile=Path(expected["caddyfile"]);disk,metadata=safe_file(caddyfile,maximum=32*1024*1024)
    baseline_disk,baseline_metadata=safe_file(backup/"Caddyfile",maximum=32*1024*1024)
    live,_=get_live(Admin());baseline_live=json.loads(safe_file(backup/"caddy-active.json")[0].decode())
    if disk!=baseline_disk or stat.S_IMODE(metadata.st_mode)!=stat.S_IMODE(baseline_metadata.st_mode) \
            or caddy_canonical(live)!=caddy_canonical(baseline_live) \
            or caddy_canonical(adapt(Admin(),disk))!=caddy_canonical(baseline_live):
        raise CleanRollbackError("clean rollback Caddy state differs")

    if not live_matches_backup(backup,Path(expected["pm2_bin"]),manifest):
        raise CleanRollbackError("clean rollback Hydro state differs")
    ordinary=json.loads(safe_file(backup/"ordinary-oj-before.json")[0].decode());validate_ordinary(ordinary)
    compare_ordinary(ordinary,collect_ordinary(expected["oj_origin"],Path(expected["pm2_bin"])))

    docker=Docker(safe_docker_socket(Path(expected["docker_socket"])))
    if docker.inspect("noi-orchestrator",allow_absent=True) is not None \
            or docker.inspect(f"noi-orchestrator-v1-old-{args.plan_id[:12]}",allow_absent=True) is not None:
        raise CleanRollbackError("clean rollback left an NOI controller")
    release=(root/"source-releases"/args.source_release).resolve(strict=True)
    if release.parent!=(root/"source-releases").resolve(strict=True):raise CleanRollbackError("clean rollback release identity differs")
    before=validate_cloud(json.loads(safe_file(backup/"cloud-before.json")[0].decode()))
    after=cloud_snapshot(release,args.desired_controller_definition,Path(args.desired_controller_definition).parent/"desired-config.yaml")
    if after!=before:raise CleanRollbackError("clean rollback cloud state differs")
    return {"status":"rollback_verified","plan_id":args.plan_id,"backup_manifest_sha256":args.backup_manifest_sha256}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--verify",action="store_true",required=True)
    parser.add_argument("--backup-directory",type=Path,required=True);parser.add_argument("--plan-id",required=True)
    parser.add_argument("--backup-manifest-sha256",required=True);parser.add_argument("--source-release",required=True)
    parser.add_argument("--expected-contract",type=Path,required=True);parser.add_argument("--desired-controller-definition",type=Path,required=True)
    args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0 or not HEX64.fullmatch(args.plan_id) \
                or not HEX64.fullmatch(args.backup_manifest_sha256) or not RELEASE.fullmatch(args.source_release):
            raise CleanRollbackError("clean rollback verification requires pinned Linux root")
        print(json.dumps(verify(args),sort_keys=True));return 0
    except (CleanRollbackError,BackupError,RestoreError,ObservationError,PostInstallError,OSError,ValueError,
            UnicodeDecodeError,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
