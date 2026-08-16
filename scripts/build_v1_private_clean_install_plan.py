#!/usr/bin/env python3
"""Build one root-only plan for the first installation on a clean OJ host."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"scripts"))
import noictl
from apply_v1_controller import LABEL_PLAN,LABEL_RELEASE,desired_definition as verify_definition,safe_private_file
from build_v1_private_upgrade_plan import (PrivatePlanError,atomic,canonical,effective_contract,
    fsync_directory,private_staging,publish_directory,qualification_image,trusted_executable,trusted_self)
from apply_v1_controller import safe_ancestors,safe_docker_socket
from stage_v1_source_release import candidate_identity,plan as source_plan
from verify_v1_clean_install_backup import verify as verify_clean_backup
from verify_v1_install_backup import safe_directory,safe_file


HEX64=re.compile(r"^[a-f0-9]{64}$")
CLEAN_STAGING_FILES={"desired-config.yaml","desired.env","desired-plugin.env","desired-plugin-token",
                     "desired-controller-definition.json","post-install-contract.json",
                     "private-clean-install-plan.json"}


def env_file(path:Path)->dict[str,str]:
    raw,_=safe_private_file(path,"desired site env",maximum=4*1024*1024);result={}
    for line in raw.decode("utf-8").splitlines():
        if not line or line.startswith("#"):continue
        match=re.fullmatch(r"([A-Z_][A-Z0-9_]*)='([^'\r\n]*)'",line)
        if match is None:raise PrivatePlanError("desired site env syntax differs")
        key,value=match.group(1),match.group(2)
        if key in result or "\x00" in value:
            raise PrivatePlanError("desired site env keys differ")
        result[key]=value
    if "HYDRO_ORCHESTRATOR_TOKEN" not in result or len(result["HYDRO_ORCHESTRATOR_TOKEN"])<32:
        raise PrivatePlanError("desired site env has no shared token")
    return result


def token_file(path:Path,environment:dict[str,str])->bytes:
    raw,_=safe_private_file(path,"desired plugin token",maximum=1024)
    token=raw.rstrip(b"\r\n")
    try:value=token.decode("utf-8")
    except UnicodeDecodeError as exc:raise PrivatePlanError("desired plugin token is not UTF-8") from exc
    if raw not in {token,token+b"\n"} or value!=environment["HYDRO_ORCHESTRATOR_TOKEN"]:
        raise PrivatePlanError("desired plugin token differs from site shared token")
    return raw


def plugin_env_file(path:Path,hydro_domain:str,frontend_domain:str)->bytes:
    raw,_=safe_private_file(path,"desired plugin env",maximum=1024*1024)
    values={}
    try:lines=raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:raise PrivatePlanError("desired plugin env is not UTF-8") from exc
    for line in lines:
        match=re.fullmatch(r"([A-Z_][A-Z0-9_]*)='([^'\r\n]*)'",line)
        if match is None or match.group(1) in values:raise PrivatePlanError("desired plugin env syntax differs")
        values[match.group(1)]=match.group(2)
    expected={
      "ORCHESTRATOR_TOKEN_FILE":"/root/.hydro/orchestrator-token","ORCHESTRATOR_DOMAIN":hydro_domain,
      "ORCHESTRATOR_MAX_CODE_BYTES":"524288","ORCHESTRATOR_IDEMPOTENCY_FILE":"/root/.hydro/orchestrator-state/submissions.json",
      "ORCHESTRATOR_IDEMPOTENCY_MAX_ENTRIES":"20000",
      "ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE":"/root/.hydro/orchestrator-state/notifications.json",
      "ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_MAX_ENTRIES":"20000",
      "ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE":"/root/.hydro/orchestrator-state/problem-drafts.json",
      "ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_MAX_ENTRIES":"2000",
      "ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE":"/root/.hydro/orchestrator-state/materials.json",
      "ORCHESTRATOR_MATERIAL_IDEMPOTENCY_MAX_ENTRIES":"2000","ORCHESTRATOR_MATERIAL_MAX_BYTES":"201326592",
      "ORCHESTRATOR_NOTIFY_ALLOWED_HTTPS_HOSTS":frontend_domain,
    }
    if values!=expected:raise PrivatePlanError("desired plugin env contract differs")
    return raw


def public_identity(config:Path,environment:dict,candidate:dict,scope:str):
    previous=os.environ.copy()
    try:
        os.environ.clear();os.environ.update(environment);state=noictl._load_config_state(config)
    finally:os.environ.clear();os.environ.update(previous)
    identity={"schema_version":1,"operation":"install","scope":scope,
        "source_revision":candidate["revision"],"source_tree":candidate["tree"],
        "candidate_manifest_sha256":candidate["manifest_sha256"],
        "source_archive_sha256":candidate["archive_sha256"],
        "configuration_binding":noictl._installation_config_binding(state)}
    return hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(",",":")).encode()).hexdigest(),state


def trusted_mount(path_text:str,label:str,want_directory:bool,*,private:bool=False)->Path:
    path=Path(path_text)
    if not path.is_absolute():raise PrivatePlanError(f"{label} mount source is not absolute")
    safe_ancestors(path,label);info=os.lstat(path)
    correct=stat.S_ISDIR(info.st_mode) if want_directory else stat.S_ISREG(info.st_mode) and info.st_nlink==1
    if not correct or stat.S_ISLNK(info.st_mode) or (platform.system().lower()=="linux" and
            (info.st_uid!=0 or stat.S_IMODE(info.st_mode)&(0o077 if private else 0o022))):
        raise PrivatePlanError(f"{label} mount source metadata differs")
    return path.resolve(strict=True)


def desired_from_template(path:Path,plan_id:str,release:str,image_id:str,root:Path,environment:dict)->dict:
    try:template=json.loads(safe_private_file(path,"controller template",maximum=2*1024*1024)[0].decode())
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise PrivatePlanError("controller template is invalid") from exc
    if not isinstance(template,dict) or set(template)!={"config","host_config"} \
            or not isinstance(template["config"],dict) or not isinstance(template["host_config"],dict):
        raise PrivatePlanError("controller template field set differs")
    config=json.loads(json.dumps(template["config"]));host=json.loads(json.dumps(template["host_config"]))
    if set(config)!={"Env","Labels","WorkingDir"} or config.get("Labels")!={} or config.get("WorkingDir")!="/app":
        raise PrivatePlanError("controller template config contract differs")
    host_keys={"Binds","NetworkMode","RestartPolicy","Privileged","ReadonlyRootfs","CapDrop","SecurityOpt","Tmpfs","Init"}
    if set(host)!=host_keys or host.get("NetworkMode")!="host" \
            or host.get("RestartPolicy")!={"Name":"unless-stopped","MaximumRetryCount":0} \
            or host.get("Privileged") is not False or host.get("Init") is not True:
        raise PrivatePlanError("controller template safety contract differs")
    config["Labels"]={LABEL_PLAN:plan_id,LABEL_RELEASE:release};config["Image"]=image_id
    mount_variables=("FRONTEND_CADDY_DIR","CONTEST_SSH_KEY","CONTEST_KNOWN_HOSTS","ARTIFACT_TOOLS_DIR")
    if any(not environment.get(name) for name in mount_variables):raise PrivatePlanError("desired site env misses mount source")
    mounts={"/app/config.yaml":(str(root/"orchestrator/config.yaml"),"ro"),
            "/app/data":(str(root/"orchestrator/data"),"rw"),"/app/runtime":(str(root/"orchestrator/runtime"),"rw"),
            "/app/caddy":(str(trusted_mount(environment["FRONTEND_CADDY_DIR"],"Caddy runtime",True)),"ro"),
            "/app/keys/contest.pem":(str(trusted_mount(environment["CONTEST_SSH_KEY"],"contest SSH key",False,
                                                        private=True)),"ro"),
            "/app/keys/known_hosts":(str(trusted_mount(environment["CONTEST_KNOWN_HOSTS"],"contest known hosts",False)),"ro"),
            "/opt/noi-artifact-tools":(str(trusted_mount(environment["ARTIFACT_TOOLS_DIR"],"artifact tools",True)),"ro")}
    binds=host.get("Binds");observed={}
    if not isinstance(binds,list):raise PrivatePlanError("controller template bind set differs")
    for bind in binds:
        if not isinstance(bind,str) or bind.count(":")!=2:raise PrivatePlanError("controller template bind syntax differs")
        source,target,mode=bind.rsplit(":",2)
        if target in observed:raise PrivatePlanError("controller template bind target is duplicated")
        observed[target]=(source,mode)
    if observed!=mounts:raise PrivatePlanError("controller template bind set differs")
    if host.get("ReadonlyRootfs") is not True or host.get("CapDrop")!=["ALL"] \
            or host.get("SecurityOpt") not in [["no-new-privileges"],["no-new-privileges:true"]] \
            or host.get("Tmpfs")!={"/tmp":"rw,nosuid,nodev,noexec,size=268435456"}:
        raise PrivatePlanError("controller template isolation contract differs")
    rows=config.get("Env")
    if not isinstance(rows,list):raise PrivatePlanError("controller template environment differs")
    observed={row.split("=",1)[0]:row.split("=",1)[1] for row in rows if isinstance(row,str) and "=" in row}
    if observed!=environment or len(rows)!=len(environment):
        raise PrivatePlanError("controller template environment differs from desired env")
    result={"schema_version":1,"plan_id":plan_id,"source_release":release,"image_id":image_id,
            "config":config,"host_config":host}
    if b"/var/run/docker.sock" in canonical(result) or b"/run/docker.sock" in canonical(result):
        raise PrivatePlanError("controller template mounts Docker control socket")
    return result


def build(args)->dict:
    if not all(HEX64.fullmatch(value) for value in (args.plan_id,args.expected_manifest_sha256,args.backup_manifest_sha256)):
        raise PrivatePlanError("private clean install identity is invalid")
    scope="qualification-lab" if args.qualification_lab else "production"
    for name in ("candidate","backup_directory","output_directory","install_root","site_config","site_env",
                 "plugin_env","plugin_token","controller_template","caddyfile","python_bin","bash_bin",
                 "pm2_bin","node_bin","docker_socket"):
        setattr(args,name,Path(os.path.abspath(getattr(args,name))))
    for name in ("python_bin","bash_bin","pm2_bin","node_bin"):
        setattr(args,name,trusted_executable(getattr(args,name)))
    args.docker_socket=safe_docker_socket(args.docker_socket)
    safe_ancestors(args.caddyfile,"clean install Caddyfile")
    caddy_info=os.lstat(args.caddyfile)
    if stat.S_ISLNK(caddy_info.st_mode) or not stat.S_ISREG(caddy_info.st_mode) or caddy_info.st_nlink!=1 \
            or (platform.system().lower()=="linux" and caddy_info.st_uid!=0):
        raise PrivatePlanError("clean install Caddyfile metadata differs")
    candidate=args.candidate.resolve(strict=True);backup=safe_directory(args.backup_directory)
    clean=verify_clean_backup(backup,args.plan_id)
    if clean["manifest_sha256"]!=args.backup_manifest_sha256:raise PrivatePlanError("clean backup trust pin differs")
    manifest,_,candidate_row=candidate_identity(candidate,args.expected_manifest_sha256,
                                                require_production=not args.qualification_lab)
    # _load_config_state uses a no-follow descriptor, but the private install
    # contract additionally requires a root-only file and trusted ancestors.
    # Prove that stronger property before parsing or expanding any YAML value.
    safe_private_file(args.site_config,"desired site config",maximum=4*1024*1024)
    environment=env_file(args.site_env);plugin_token_raw=token_file(args.plugin_token,environment)
    public_id,state=public_identity(args.site_config,environment,candidate_row,scope)
    if public_id!=args.plan_id:raise PrivatePlanError("public install plan ID differs")
    oj_origin,exam_origin,hydro_domain,hydro_domain_id,frontend_domain=effective_contract(state)
    plugin_env_raw=plugin_env_file(args.plugin_env,hydro_domain_id,frontend_domain)
    image_id=qualification_image(candidate,manifest,args.controller_image_id,not args.qualification_lab)
    install_root=Path(os.path.abspath(args.install_root));staged=source_plan(candidate,args.expected_manifest_sha256,
        install_root,qualification_lab=args.qualification_lab,owner_plan_id=args.plan_id)
    final,output=private_staging(args.output_directory,args.plan_id,allowed_files=CLEAN_STAGING_FILES,
                                 operation_slug="clean-install")
    transaction=output/"transaction";transaction.mkdir(mode=0o700)
    os.chmod(transaction,0o700);fsync_directory(output)
    copies={"desired-config.yaml":args.site_config,"desired.env":args.site_env,
            "desired-plugin.env":args.plugin_env,"desired-plugin-token":args.plugin_token}
    for name,source in copies.items():
        raw=plugin_token_raw if name=="desired-plugin-token" else plugin_env_raw if name=="desired-plugin.env" \
            else safe_private_file(source,name)[0]
        atomic(output/name,raw)
    desired=desired_from_template(args.controller_template,args.plan_id,staged["release_name"],image_id,install_root,environment)
    atomic(output/"desired-controller-definition.json",canonical(desired))
    paths={name:final/name for name in copies};project=install_root/"orchestrator"
    contract={"schema_version":1,"plan_id":args.plan_id,"source_release":staged["release_name"],
        "controller_image_id":image_id,"oj_origin":oj_origin,"exam_origin":exam_origin,
        "source_pointer":str(install_root/"current-source"),"caddyfile":str(args.caddyfile),
        "snippet":str(project/"runtime/caddy-exam.conf"),"project_config":str(project/"config.yaml"),
        "project_env":str(project/".env"),"database":str(project/"data/orchestrator.db"),
        "pm2_bin":str(args.pm2_bin),"docker_socket":str(args.docker_socket)}
    atomic(output/"post-install-contract.json",canonical(contract))
    artifact_paths={"expected_contract":output/"post-install-contract.json",
        "desired_controller_definition":output/"desired-controller-definition.json","desired_config":output/"desired-config.yaml",
        "desired_env":output/"desired.env","desired_plugin_env":output/"desired-plugin.env",
        "desired_plugin_token":output/"desired-plugin-token"}
    private_hashes={key:hashlib.sha256(safe_private_file(path,key)[0]).hexdigest() for key,path in artifact_paths.items()}
    plan={"schema_version":1,"operation":"clean-install","plan_id":args.plan_id,"scope":scope,
        "source_plan_id":staged["plan_id"],"source_release":staged["release_name"],"candidate":str(candidate),
        "candidate_manifest_sha256":args.expected_manifest_sha256,"backup_directory":str(backup),
        "backup_manifest_sha256":args.backup_manifest_sha256,"transaction_directory":str(final/"transaction"),
        "install_root":str(install_root),"expected_contract":str(final/"post-install-contract.json"),
        "private_artifact_sha256":private_hashes,
        "desired_controller_definition":str(final/"desired-controller-definition.json"),
        "desired_config":str(paths["desired-config.yaml"]),"desired_env":str(paths["desired.env"]),
        "desired_plugin_env":str(paths["desired-plugin.env"]),"desired_plugin_token":str(paths["desired-plugin-token"]),
        "project_config":str(project/"config.yaml"),"project_env":str(project/".env"),
        "database":str(project/"data/orchestrator.db"),"caddyfile":str(args.caddyfile),
        "snippet":str(project/"runtime/caddy-exam.conf"),"hydro_domain":hydro_domain,
        "frontend_domain":frontend_domain,"orchestrator_upstream":"http://127.0.0.1:8600",
        "executables":{"python":str(args.python_bin),"bash":str(args.bash_bin),"pm2":str(args.pm2_bin),
                       "node":str(args.node_bin),"docker_socket":str(args.docker_socket)}}
    atomic(output/"private-clean-install-plan.json",canonical(plan));publish_directory(output,final)
    raw,_=safe_private_file(final/"private-clean-install-plan.json","private clean install plan")
    from apply_v1_clean_install import load_plan as verify_plan,verify_bindings
    verified=verify_plan(final/"private-clean-install-plan.json",hashlib.sha256(raw).hexdigest())
    verify_bindings(verified)
    verify_definition(final/"desired-controller-definition.json",args.plan_id,staged["release_name"])
    return {"status":"planned","operation":"clean-install","plan_id":args.plan_id,
            "private_plan":str(final/"private-clean-install-plan.json"),
            "private_plan_sha256":hashlib.sha256(raw).hexdigest(),"source_plan_id":staged["plan_id"],
            "source_release":staged["release_name"],"service_mutations":0}


def parser():
    p=argparse.ArgumentParser()
    for name in ("plan-id","expected-manifest-sha256","controller-image-id","backup-manifest-sha256"):p.add_argument(f"--{name}",required=True)
    for name in ("candidate","backup-directory","output-directory","install-root","site-config","site-env","plugin-env",
                 "plugin-token","controller-template","caddyfile","python-bin","bash-bin","pm2-bin","node-bin","docker-socket"):
        p.add_argument(f"--{name}",required=True,type=Path)
    p.add_argument("--qualification-lab",action="store_true");return p


def main():
    args=parser().parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0:raise PrivatePlanError("clean planning requires Linux root")
        trusted_self()
        print(json.dumps(build(args),sort_keys=True));return 0
    except (PrivatePlanError,OSError,ValueError,UnicodeDecodeError,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
