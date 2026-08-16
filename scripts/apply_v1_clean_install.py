#!/usr/bin/env python3
"""Run the only supported V1 transaction on a target with no prior NOI install."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"scripts"))
from apply_v1_install import (ApplyInstallError,HEX64,RELEASE,DOMAIN,absolute,exact)
from apply_v1_controller import safe_private_file
from orchestrator.services.install_phase_drivers import (CleanFinalRollbackVerifier,CleanMaterialsDriver,
    ClosedFrontendDriver,ControllerDriver,HydroIntegrationDriver,PostInstallVerificationDriver,SourceReleaseDriver)
from orchestrator.services.install_transaction import InstallTransactionError,run_clean
from verify_v1_install_backup import safe_directory,safe_file,validate_manifest
from verify_v1_clean_install_backup import validate_clean_target


PATH_KEYS=("candidate","backup_directory","transaction_directory","install_root","expected_contract",
           "desired_controller_definition","desired_config","desired_env","desired_plugin_env",
           "desired_plugin_token","project_config","project_env","database","caddyfile","snippet")


def trusted_self()->None:
    path=Path(os.path.abspath(__file__));metadata=os.lstat(path)
    if path!=path.resolve(strict=True) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1:
        raise ApplyInstallError("clean install executor metadata is unsafe")
    if platform.system().lower()=="linux":
        if metadata.st_uid!=0 or stat.S_IMODE(metadata.st_mode)&0o022:
            raise ApplyInstallError("clean install executor is not trusted")
        current=path.parent
        while True:
            row=os.lstat(current)
            if stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode) or row.st_uid!=0 or stat.S_IMODE(row.st_mode)&0o022:
                raise ApplyInstallError("clean install executor ancestor is unsafe")
            if current.parent==current:break
            current=current.parent


def load_plan(path:Path,expected_sha:str)->dict:
    raw,_=safe_private_file(path,"private clean install plan",maximum=2*1024*1024)
    if hashlib.sha256(raw).hexdigest()!=expected_sha:raise ApplyInstallError("private clean install plan trust pin differs")
    try:value=json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise ApplyInstallError("private clean install plan is invalid JSON") from exc
    keys={"schema_version","operation","plan_id","scope","source_plan_id","source_release","candidate",
          "candidate_manifest_sha256","backup_directory","backup_manifest_sha256","transaction_directory",
          "install_root","expected_contract","desired_controller_definition","desired_config","desired_env",
          "private_artifact_sha256",
          "desired_plugin_env","desired_plugin_token","project_config","project_env","database","caddyfile",
          "snippet","hydro_domain","frontend_domain","orchestrator_upstream","executables"}
    row=exact(value,keys,"private clean install plan")
    if row["schema_version"]!=1 or row["operation"]!="clean-install" \
            or row["scope"] not in {"production","qualification-lab"} \
            or not HEX64.fullmatch(str(row["plan_id"])) or not HEX64.fullmatch(str(row["source_plan_id"])) \
            or not RELEASE.fullmatch(str(row["source_release"])) \
            or not HEX64.fullmatch(str(row["candidate_manifest_sha256"])) \
            or not HEX64.fullmatch(str(row["backup_manifest_sha256"])):
        raise ApplyInstallError("private clean install plan identity differs")
    for name in ("hydro_domain","frontend_domain"):
        if not isinstance(row[name],str) or not DOMAIN.fullmatch(row[name]) or "." not in row[name]:
            raise ApplyInstallError("private clean install domain differs")
    if row["hydro_domain"].lower()==row["frontend_domain"].lower() \
            or row["orchestrator_upstream"]!="http://127.0.0.1:8600":
        raise ApplyInstallError("private clean install frontend contract differs")
    for name in PATH_KEYS:absolute(row[name],name)
    executables=exact(row["executables"],{"python","bash","pm2","node","docker_socket"},"private clean install executables")
    hashes=exact(row["private_artifact_sha256"],{"expected_contract","desired_controller_definition","desired_config","desired_env",
        "desired_plugin_env","desired_plugin_token"},"private clean install artifact hashes")
    if any(not HEX64.fullmatch(str(value)) for value in hashes.values()):raise ApplyInstallError("private clean artifact hash differs")
    for name,value in executables.items():absolute(value,name)
    root=Path(row["install_root"]);private=Path(path).resolve(strict=True).parent
    expected={"project_config":root/"orchestrator/config.yaml","project_env":root/"orchestrator/.env",
              "database":root/"orchestrator/data/orchestrator.db","snippet":root/"orchestrator/runtime/caddy-exam.conf"}
    if any(Path(row[key])!=value for key,value in expected.items()):raise ApplyInstallError("clean install target layout differs")
    private_paths={"transaction_directory":private/"transaction","expected_contract":private/"post-install-contract.json",
        "desired_controller_definition":private/"desired-controller-definition.json","desired_config":private/"desired-config.yaml",
        "desired_env":private/"desired.env","desired_plugin_env":private/"desired-plugin.env",
        "desired_plugin_token":private/"desired-plugin-token"}
    if any(Path(row[key])!=value for key,value in private_paths.items()):raise ApplyInstallError("private clean plan layout differs")
    return row


def verify_bindings(row:dict)->dict:
    backup=safe_directory(Path(row["backup_directory"]));manifest_raw,_=safe_file(backup/"backup-manifest.json",maximum=4*1024*1024)
    if hashlib.sha256(manifest_raw).hexdigest()!=row["backup_manifest_sha256"]:raise ApplyInstallError("clean install backup trust pin differs")
    manifest=validate_manifest(json.loads(manifest_raw.decode()),backup,expected_plan_id=row["plan_id"])
    if manifest.get("operation")!="clean-install":raise ApplyInstallError("private plan does not use a clean baseline")
    for key in ("expected_contract","desired_controller_definition","desired_config","desired_env","desired_plugin_env","desired_plugin_token"):
        raw,_=safe_private_file(Path(row[key]),f"private clean artifact {key}")
        if hashlib.sha256(raw).hexdigest()!=row["private_artifact_sha256"][key]:
            raise ApplyInstallError("private clean artifact content differs")
    target=validate_clean_target(json.loads(safe_file(backup/"clean-target.json")[0].decode()))
    clean_expected={"install_root":row["install_root"],"source_pointer":str(Path(row["install_root"])/"current-source"),
        "project_config":row["project_config"],"project_env":row["project_env"],"database":row["database"],
        "database_wal":row["database"]+"-wal","database_shm":row["database"]+"-shm","caddy_snippet":row["snippet"],
        "hydro_addon_tree":"/root/.hydro/addons/orchestrator-submit","hydro_plugin_env":"/root/.hydro/orchestrator-plugin.env",
        "hydro_plugin_token":"/root/.hydro/orchestrator-token","hydro_plugin_state":"/root/.hydro/orchestrator-state"}
    if any(target["paths"][key]!={"path":value,"present":False} for key,value in clean_expected.items()):
        raise ApplyInstallError("clean target baseline differs from private plan")
    contract=json.loads(safe_file(Path(row["expected_contract"]))[0].decode())
    required={"schema_version","plan_id","source_release","controller_image_id","oj_origin","exam_origin",
              "source_pointer","caddyfile","snippet","project_config","project_env","database","pm2_bin","docker_socket"}
    if not isinstance(contract,dict) or set(contract)!=required or contract["schema_version"]!=1 \
            or contract["plan_id"]!=row["plan_id"] or contract["source_release"]!=row["source_release"] \
            or contract["source_pointer"]!=clean_expected["source_pointer"] or contract["caddyfile"]!=row["caddyfile"] \
            or contract["snippet"]!=row["snippet"] or contract["project_config"]!=row["project_config"] \
            or contract["project_env"]!=row["project_env"] or contract["database"]!=row["database"] \
            or contract["pm2_bin"]!=row["executables"]["pm2"] \
            or contract["docker_socket"]!=row["executables"]["docker_socket"]:
        raise ApplyInstallError("clean post-install contract differs")
    desired=json.loads(safe_private_file(Path(row["desired_controller_definition"]),"desired controller definition")[0].decode())
    if not isinstance(desired,dict) or desired.get("plan_id")!=row["plan_id"] \
            or desired.get("source_release")!=row["source_release"] or desired.get("image_id")!=contract["controller_image_id"]:
        raise ApplyInstallError("clean desired controller identity differs")
    return contract


def drivers(row:dict,contract:dict):
    install=Path(row["install_root"]);backup=Path(row["backup_directory"]);release=row["source_release"];exe=row["executables"]
    python=Path(exe["python"]);expected=Path(row["expected_contract"])
    phase={
      "source_release":SourceReleaseDriver(Path(row["candidate"]),row["candidate_manifest_sha256"],install,row["source_plan_id"],
        ROOT/"scripts/stage_v1_source_release.py",expected_source_release=release,
        qualification_lab=row["scope"]=="qualification-lab",python_executable=python),
      "clean_materials":CleanMaterialsDriver(install,release,backup,Path(row["desired_config"]),Path(row["desired_env"]),
        Path(row["desired_plugin_env"]),Path(row["desired_plugin_token"]),python_executable=python),
      "hydro_integration":HydroIntegrationDriver(install,release,backup,pm2_bin=Path(exe["pm2"]),node_bin=Path(exe["node"]),
        python_executable=python,bash_executable=Path(exe["bash"])),
      "closed_frontend":ClosedFrontendDriver(install,release,backup,Path(row["caddyfile"]),Path(row["snippet"]),
        row["hydro_domain"],row["frontend_domain"],row["orchestrator_upstream"],python_executable=python),
      "controller":ControllerDriver(install,release,backup,Path(row["desired_controller_definition"]),
        Path(row["desired_config"]),Path(row["desired_env"]),Path(row["project_config"]),Path(row["project_env"]),
        Path(row["database"]),docker_socket=Path(exe["docker_socket"]),python_executable=python),
      "post_install_verification":PostInstallVerificationDriver(install,release,backup,expected,python_executable=python),
    }
    final=CleanFinalRollbackVerifier(install,release,backup,expected,Path(row["desired_controller_definition"]),
        python_executable=python)
    return phase,final


def apply(row:dict)->dict:
    contract=verify_bindings(row);phase,final=drivers(row,contract)
    result=run_clean(Path(row["transaction_directory"]),row["plan_id"],row["backup_manifest_sha256"],phase,final)
    if result["status"] not in {"committed","rollback_verified"}:raise ApplyInstallError("clean install terminal status differs")
    return {"status":result["status"],"operation":"clean-install","plan_id":row["plan_id"],
            "backup_manifest_sha256":row["backup_manifest_sha256"]}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--apply",action="store_true",required=True)
    parser.add_argument("--private-plan",required=True,type=Path);parser.add_argument("--expected-plan-sha256",required=True);args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0 or not HEX64.fullmatch(args.expected_plan_sha256):
            raise ApplyInstallError("V1 clean install apply requires pinned Linux root")
        trusted_self();row=load_plan(args.private_plan,args.expected_plan_sha256);print(json.dumps(apply(row),sort_keys=True));return 0
    except (ApplyInstallError,InstallTransactionError,OSError,ValueError,UnicodeDecodeError,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
