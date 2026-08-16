#!/usr/bin/env python3
"""Run the only supported six-phase V1 production upgrade transaction."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import sys


ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"scripts"))

from orchestrator.services.install_phase_drivers import (ClosedFrontendDriver,ControllerDriver,ControllerQuiesceDriver,
    FinalRollbackVerifier,HydroIntegrationDriver,PostInstallVerificationDriver,SourceReleaseDriver)
from orchestrator.services.install_transaction import InstallTransactionError,run
from apply_v1_controller import safe_private_file
from verify_v1_install_backup import safe_directory,safe_file,validate_manifest


HEX64=re.compile(r"^[a-f0-9]{64}$");RELEASE=re.compile(r"^[a-f0-9]{40}-[a-f0-9]{12}$")
DOMAIN=re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


class ApplyInstallError(RuntimeError):pass


def exact(value,keys,label):
    if not isinstance(value,dict) or set(value)!=set(keys):raise ApplyInstallError(f"{label} field set differs")
    return value


def trusted_self()->None:
    path=Path(os.path.abspath(__file__));metadata=os.lstat(path)
    if path!=path.resolve(strict=True) or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1:
        raise ApplyInstallError("install executor metadata is unsafe")
    if platform.system().lower()=="linux":
        current=path.parent
        if metadata.st_uid!=0 or stat.S_IMODE(metadata.st_mode)&0o022:raise ApplyInstallError("install executor is not trusted")
        while True:
            row=os.lstat(current)
            if stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode) or row.st_uid!=0 or stat.S_IMODE(row.st_mode)&0o022:
                raise ApplyInstallError("install executor ancestor is unsafe")
            if current.parent==current:break
            current=current.parent


def absolute(value,label)->Path:
    if not isinstance(value,str) or "\x00" in value or not PurePosixPath(value).is_absolute():
        raise ApplyInstallError(f"private install path differs: {label}")
    return Path(value)


def load_plan(path:Path,expected_sha:str)->dict:
    raw,_=safe_private_file(path,"private install plan",maximum=2*1024*1024)
    if hashlib.sha256(raw).hexdigest()!=expected_sha:raise ApplyInstallError("private install plan trust pin differs")
    try:value=json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise ApplyInstallError("private install plan is invalid JSON") from exc
    keys={"schema_version","operation","plan_id","scope","source_plan_id","source_release","candidate","candidate_manifest_sha256",
          "backup_directory","backup_manifest_sha256","transaction_directory","install_root","expected_contract",
          "private_artifact_sha256",
          "desired_controller_definition","desired_config","desired_env","project_config","project_env","database",
          "caddyfile","snippet","hydro_domain","frontend_domain","orchestrator_upstream","executables"}
    row=exact(value,keys,"private install plan")
    if row["schema_version"]!=1 or row["operation"]!="upgrade" or row["scope"] not in {"production","qualification-lab"} \
            or not HEX64.fullmatch(str(row["plan_id"])) or not HEX64.fullmatch(str(row["source_plan_id"])) \
            or not RELEASE.fullmatch(str(row["source_release"])) \
            or not HEX64.fullmatch(str(row["candidate_manifest_sha256"])) \
            or not HEX64.fullmatch(str(row["backup_manifest_sha256"])):
        raise ApplyInstallError("private install plan identity differs")
    for domain in ("hydro_domain","frontend_domain"):
        if not isinstance(row[domain],str) or not DOMAIN.fullmatch(row[domain]) or "." not in row[domain]:
            raise ApplyInstallError("private install domain differs")
    if row["hydro_domain"].lower()==row["frontend_domain"].lower() or row["orchestrator_upstream"]!="http://127.0.0.1:8600":
        raise ApplyInstallError("private install frontend contract differs")
    path_keys=("candidate","backup_directory","transaction_directory","install_root","expected_contract",
               "desired_controller_definition","desired_config","desired_env","project_config","project_env","database","caddyfile","snippet")
    for name in path_keys:absolute(row[name],name)
    executables=exact(row["executables"],{"python","bash","pm2","node","docker_socket"},"private install executables")
    hashes=exact(row["private_artifact_sha256"],{"expected_contract","desired_controller_definition","desired_config","desired_env"},
                 "private install artifact hashes")
    if any(not HEX64.fullmatch(str(value)) for value in hashes.values()):raise ApplyInstallError("private install artifact hash differs")
    for name,value in executables.items():absolute(value,name)
    return row


def verify_bindings(row:dict)->None:
    backup=safe_directory(Path(row["backup_directory"]));manifest_raw,_=safe_file(backup/"backup-manifest.json",maximum=4*1024*1024)
    if hashlib.sha256(manifest_raw).hexdigest()!=row["backup_manifest_sha256"]:raise ApplyInstallError("install backup trust pin differs")
    manifest=json.loads(manifest_raw.decode("utf-8"));validate_manifest(manifest,backup,expected_plan_id=row["plan_id"])
    for key in ("expected_contract","desired_controller_definition","desired_config","desired_env"):
        raw,_=safe_private_file(Path(row[key]),f"private install artifact {key}")
        if hashlib.sha256(raw).hexdigest()!=row["private_artifact_sha256"][key]:
            raise ApplyInstallError("private install artifact content differs")
    contract_raw,_=safe_file(Path(row["expected_contract"]),maximum=1024*1024)
    try:contract=json.loads(contract_raw.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise ApplyInstallError("post-install contract is invalid JSON") from exc
    if not isinstance(contract,dict) or contract.get("plan_id")!=row["plan_id"] or contract.get("source_release")!=row["source_release"] \
            or contract.get("project_config")!=row["project_config"] or contract.get("project_env")!=row["project_env"] \
            or contract.get("database")!=row["database"] or contract.get("caddyfile")!=row["caddyfile"] \
            or contract.get("snippet")!=row["snippet"] or contract.get("pm2_bin")!=row["executables"]["pm2"] \
            or contract.get("docker_socket")!=row["executables"]["docker_socket"]:
        raise ApplyInstallError("post-install contract differs from private install plan")
    desired_raw,_=safe_private_file(Path(row["desired_controller_definition"]),"desired controller definition",maximum=1024*1024)
    try:desired=json.loads(desired_raw.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise ApplyInstallError("desired controller definition is invalid JSON") from exc
    if not isinstance(desired,dict) or desired.get("plan_id")!=row["plan_id"] \
            or desired.get("source_release")!=row["source_release"] \
            or desired.get("image_id")!=contract.get("controller_image_id"):
        raise ApplyInstallError("desired controller identity differs from private install plan")


def drivers(row:dict,contract_row:dict|None=None):
    install=Path(row["install_root"]);backup=Path(row["backup_directory"]);release=row["source_release"];exe=row["executables"]
    python=Path(exe["python"]);expected=Path(row["expected_contract"])
    if contract_row is None:
        try:contract_row=json.loads(safe_file(expected)[0].decode("utf-8"))
        except (OSError,UnicodeDecodeError,json.JSONDecodeError) as exc:raise ApplyInstallError("post-install contract is unavailable") from exc
    result={
      "source_release":SourceReleaseDriver(candidate=Path(row["candidate"]),expected_manifest_sha256=row["candidate_manifest_sha256"],
        install_root=install,source_plan_id=row["source_plan_id"],transaction_script=ROOT/"scripts/stage_v1_source_release.py",
        expected_source_release=release,
        qualification_lab=row["scope"]=="qualification-lab",python_executable=python),
      "controller_quiesce":ControllerQuiesceDriver(install_root=install,source_release_name=release,
        backup_directory=backup,project_config=Path(row["project_config"]),project_env=Path(row["project_env"]),
        database=Path(row["database"]),caddyfile=Path(row["caddyfile"]),snippet=Path(row["snippet"]),
        pm2_bin=Path(exe["pm2"]),oj_origin=contract_row["oj_origin"],
        docker_socket=Path(exe["docker_socket"]),python_executable=python),
      "hydro_integration":HydroIntegrationDriver(install_root=install,source_release_name=release,backup_directory=backup,
        pm2_bin=Path(exe["pm2"]),node_bin=Path(exe["node"]),python_executable=python,bash_executable=Path(exe["bash"])),
      "closed_frontend":ClosedFrontendDriver(install_root=install,source_release_name=release,backup_directory=backup,
        caddyfile=Path(row["caddyfile"]),snippet=Path(row["snippet"]),hydro_domain=row["hydro_domain"],
        frontend_domain=row["frontend_domain"],orchestrator_upstream=row["orchestrator_upstream"],python_executable=python),
      "controller":ControllerDriver(install_root=install,source_release_name=release,backup_directory=backup,
        desired_definition=Path(row["desired_controller_definition"]),desired_config=Path(row["desired_config"]),
        desired_env=Path(row["desired_env"]),project_config=Path(row["project_config"]),project_env=Path(row["project_env"]),
        database=Path(row["database"]),docker_socket=Path(exe["docker_socket"]),python_executable=python),
      "post_install_verification":PostInstallVerificationDriver(install_root=install,source_release_name=release,
        backup_directory=backup,expected_contract=expected,python_executable=python),
    }
    final=FinalRollbackVerifier(install,release,backup,expected,python_executable=python)
    return result,final


def apply(row:dict)->dict:
    verify_bindings(row)
    contract_row=json.loads(safe_file(Path(row["expected_contract"]))[0].decode("utf-8"))
    phase_drivers,final=drivers(row,contract_row)
    result=run(Path(row["transaction_directory"]),row["plan_id"],row["backup_manifest_sha256"],phase_drivers,final)
    if result["status"] not in {"committed","rollback_verified"}:raise ApplyInstallError("service install terminal status differs")
    return {"status":result["status"],"plan_id":row["plan_id"],"backup_manifest_sha256":row["backup_manifest_sha256"]}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--apply",action="store_true",required=True)
    parser.add_argument("--private-plan",required=True,type=Path);parser.add_argument("--expected-plan-sha256",required=True);args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0 or not HEX64.fullmatch(args.expected_plan_sha256):
            raise ApplyInstallError("V1 install apply requires pinned Linux root")
        trusted_self();row=load_plan(args.private_plan,args.expected_plan_sha256);print(json.dumps(apply(row),sort_keys=True));return 0
    except (ApplyInstallError,InstallTransactionError,OSError,ValueError,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
