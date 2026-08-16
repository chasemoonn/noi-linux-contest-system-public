#!/usr/bin/env python3
"""Install or roll back only the NOI controller container."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import socket
import sqlite3
import stat
import sys
import tempfile
import time
from urllib.parse import quote

from build_v1_controller_install_backup import immutable_identity
from verify_v1_controller_install_backup import canonical, validate_definition, validate_image
from verify_v1_install_backup import BackupError, safe_directory, safe_file, validate_manifest


HEX64=re.compile(r"^[a-f0-9]{64}$"); RELEASE=re.compile(r"^[a-f0-9]{40}-[a-f0-9]{12}$")
LABEL_PLAN="org.noi.install.plan"; LABEL_RELEASE="org.noi.source.release"


class ControllerPhaseError(RuntimeError): pass


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self,path:Path,timeout=15): super().__init__("localhost",timeout=timeout); self.path=str(path)
    def connect(self): self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); self.sock.settimeout(self.timeout); self.sock.connect(self.path)


class Docker:
    def __init__(self,path:Path): self.path=path
    def request(self,method,path,body=None,expected={200},timeout=15):
        connection=UnixHTTPConnection(self.path,timeout=timeout)
        headers={"Content-Type":"application/json"} if body is not None else {}
        raw_body=json.dumps(body,separators=(",",":"),allow_nan=False).encode() if body is not None else None
        try:
            connection.request(method,path,body=raw_body,headers=headers); response=connection.getresponse(); raw=response.read(16*1024*1024+1)
        except (OSError,http.client.HTTPException) as exc: raise ControllerPhaseError("controller Docker request failed") from exc
        finally: connection.close()
        if response.status not in expected or len(raw)>16*1024*1024: raise ControllerPhaseError("controller Docker response differs")
        return response.status,raw
    def inspect(self,name,allow_absent=False):
        status,raw=self.request("GET",f"/containers/{quote(name)}/json",expected={200,404} if allow_absent else {200})
        if status==404:return None
        try:value=json.loads(raw)
        except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise ControllerPhaseError("controller inspect is invalid") from exc
        if not isinstance(value,dict):raise ControllerPhaseError("controller inspect differs")
        return value
    def image(self,image_id):
        _,raw=self.request("GET",f"/images/{quote(image_id,safe='')}/json")
        try:value=json.loads(raw)
        except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise ControllerPhaseError("controller image inspect is invalid") from exc
        if not isinstance(value,dict) or value.get("Id")!=image_id:
            raise ControllerPhaseError("controller image identity differs")
        return value
    # The controller joins an in-flight judge worker for up to 40 seconds and
    # then performs fail-closed cloud/Caddy cleanup.  Never truncate that
    # orderly shutdown with Docker's shorter default timeout.
    def stop(self,container_id): self.request("POST",f"/containers/{container_id}/stop?t=90",expected={204,304},timeout=105)
    def start(self,container_id): self.request("POST",f"/containers/{container_id}/start",expected={204,304})
    def rename(self,container_id,name): self.request("POST",f"/containers/{container_id}/rename?name={quote(name)}",expected={204})
    def remove(self,container_id): self.request("DELETE",f"/containers/{container_id}?v=0&force=0",expected={204})
    def create(self,name,body):
        _,raw=self.request("POST",f"/containers/create?name={quote(name)}",body=body,expected={201})
        try:value=json.loads(raw); container_id=value["Id"]
        except (UnicodeDecodeError,json.JSONDecodeError,KeyError,TypeError) as exc:raise ControllerPhaseError("controller create response differs") from exc
        if not HEX64.fullmatch(str(container_id)):raise ControllerPhaseError("created controller ID differs")
        return container_id


def safe_ancestors(path:Path,label:str):
    requested=Path(os.path.abspath(path)); parent=requested.parent
    if platform.system().lower()!="linux":return parent.resolve(strict=True)
    current=Path("/")
    for part in parent.parts[1:]:
        current=current/part;metadata=os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) \
                or metadata.st_uid!=0 or stat.S_IMODE(metadata.st_mode)&0o022:
            raise ControllerPhaseError(f"{label} ancestor is unsafe")
    return parent.resolve(strict=True)


def safe_private_file(path:Path,label:str,maximum=32*1024*1024):
    requested=Path(os.path.abspath(path));safe_ancestors(requested,label)
    raw,metadata=safe_file(requested,maximum=maximum)
    if platform.system().lower()=="linux" and stat.S_IMODE(metadata.st_mode)&0o077:
        raise ControllerPhaseError(f"{label} mode differs")
    return raw,metadata


def load_json(path:Path,label:str,maximum=32*1024*1024,private=False):
    try:
        raw=(safe_private_file(path,label,maximum) if private else safe_file(path,maximum=maximum))[0]
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise ControllerPhaseError(f"{label} is invalid JSON") from exc


def backup_inputs(directory:Path,plan_id:str,manifest_sha:str):
    root=safe_directory(directory); raw,_=safe_file(root/"backup-manifest.json",maximum=4*1024*1024)
    if hashlib.sha256(raw).hexdigest()!=manifest_sha:raise ControllerPhaseError("backup manifest trust pin differs")
    manifest=json.loads(raw.decode());validate_manifest(manifest,root,expected_plan_id=plan_id)
    definition=validate_definition(load_json(root/"controller-definition.json","controller definition"))
    validate_image(load_json(root/"controller-image.json","controller image"),definition)
    return root,manifest,definition


def desired_definition(path:Path,plan_id:str,release:str):
    row=load_json(path,"desired controller definition",private=True)
    if not isinstance(row,dict) or set(row)!={"schema_version","plan_id","source_release","image_id","config","host_config"} \
            or row["schema_version"]!=1 or row["plan_id"]!=plan_id or row["source_release"]!=release \
            or not re.fullmatch(r"sha256:[a-f0-9]{64}",str(row["image_id"])) \
            or not isinstance(row["config"],dict) or not isinstance(row["host_config"],dict):
        raise ControllerPhaseError("desired controller definition differs")
    config=row["config"];host=row["host_config"];labels=config.get("Labels")
    if config.get("Image")!=row["image_id"] or not isinstance(labels,dict) \
            or labels.get(LABEL_PLAN)!=plan_id or labels.get(LABEL_RELEASE)!=release \
            or host.get("NetworkMode")!="host" or (host.get("RestartPolicy") or {}).get("Name")!="unless-stopped" \
            or host.get("Privileged") is not False:
        raise ControllerPhaseError("desired controller safety contract differs")
    raw=canonical(row)
    if b"/var/run/docker.sock" in raw or b"/run/docker.sock" in raw:
        raise ControllerPhaseError("desired controller may not mount Docker control socket")
    return row


def environment_map(value)->dict[str,str]|None:
    if value is None:return {}
    if not isinstance(value,list):return None
    result={}
    for item in value:
        if not isinstance(item,str) or "\x00" in item or "=" not in item:return None
        key,content=item.split("=",1)
        if not key or key in result:return None
        result[key]=content
    return result


def label_map(value)->dict[str,str]|None:
    if value is None:return {}
    if not isinstance(value,dict) or any(not isinstance(key,str) or not isinstance(content,str)
                                          for key,content in value.items()):return None
    return value


def created_matches_desired(value:dict,desired:dict,image:dict)->bool:
    config=value.get("Config") or {};host=value.get("HostConfig") or {}
    image_config=image.get("Config") or {}
    if image.get("Id")!=desired["image_id"] or value.get("Image")!=desired["image_id"]:return False
    for key,expected in desired["config"].items():
        if key not in {"Env","Labels"} and config.get(key)!=expected:return False
    image_environment=environment_map(image_config.get("Env"));requested_environment=environment_map(desired["config"].get("Env"))
    actual_environment=environment_map(config.get("Env"))
    if None in (image_environment,requested_environment,actual_environment):return False
    expected_environment=dict(image_environment);expected_environment.update(requested_environment)
    if actual_environment!=expected_environment:return False
    image_labels=label_map(image_config.get("Labels"));requested_labels=label_map(desired["config"].get("Labels"))
    actual_labels=label_map(config.get("Labels"))
    if None in (image_labels,requested_labels,actual_labels):return False
    expected_labels=dict(image_labels);expected_labels.update(requested_labels)
    if actual_labels!=expected_labels:return False
    if any(host.get(key)!=expected for key,expected in desired["host_config"].items()):return False
    if host.get("Privileged") is not False or host.get("NetworkMode")!="host":return False
    if (host.get("RestartPolicy") or {}).get("Name")!="unless-stopped":return False
    labels=config.get("Labels") or {}
    return labels.get(LABEL_PLAN)==desired["plan_id"] and labels.get(LABEL_RELEASE)==desired["source_release"]


def inspect_matches(value,baseline):
    container=baseline["container"]
    return value is not None and value.get("Id")==container["container_id"] \
        and value.get("Image")==container["image_id"] and value.get("Name")==container["name"] \
        and value.get("RestartCount")==container["restart_count"] \
        and hashlib.sha256(canonical(immutable_identity(value))).hexdigest()==container["immutable_identity_sha256"]


def owned(value,plan_id,release):
    labels=((value or {}).get("Config") or {}).get("Labels") or {}
    return labels.get(LABEL_PLAN)==plan_id and labels.get(LABEL_RELEASE)==release


def wait_running(docker:Docker,name:str,expected:bool,deadline=60):
    end=time.monotonic()+deadline;last=None
    while time.monotonic()<end:
        last=docker.inspect(name,allow_absent=True)
        if last is not None and (last.get("State") or {}).get("Running") is expected:return last
        time.sleep(1)
    raise ControllerPhaseError("controller lifecycle deadline expired")


def local_health(deadline=90):
    end=time.monotonic()+deadline
    while time.monotonic()<end:
        connection=http.client.HTTPConnection("127.0.0.1",8600,timeout=3)
        try:
            connection.request("GET","/healthz");response=connection.getresponse();raw=response.read(2*1024*1024+1)
            value=json.loads(raw)
            if response.status==200 and isinstance(value,dict) and value.get("ok") is True:return
        except (OSError,http.client.HTTPException,UnicodeDecodeError,json.JSONDecodeError):pass
        finally:connection.close()
        time.sleep(1)
    raise ControllerPhaseError("controller did not become healthy")


def safe_parent(path:Path):
    return safe_ancestors(path,"controller target")


def atomic_file(path:Path,raw:bytes,mode:int):
    parent=safe_parent(path);descriptor,temporary=tempfile.mkstemp(prefix=f".{path.name}.v1-",dir=parent)
    try:
        if hasattr(os,"fchmod"):os.fchmod(descriptor,mode)
        else:os.chmod(temporary,mode)
        with os.fdopen(descriptor,"wb",closefd=True) as output:output.write(raw);output.flush();os.fsync(output.fileno())
        descriptor=-1
        os.replace(temporary,path)
        if platform.system().lower()=="linux":
            directory=os.open(parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        if descriptor>=0:os.close(descriptor)
        if os.path.lexists(temporary):os.unlink(temporary)


def restore_file(backup:Path,manifest:dict,key:str,target:Path):
    entry=manifest["artifacts"][key]
    if entry["present"]:atomic_file(target,safe_file(backup/entry["filename"])[0],entry["mode"])
    elif os.path.lexists(target):
        parent=safe_parent(target);metadata=os.lstat(target)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1 \
                or (platform.system().lower()=="linux" and metadata.st_uid!=0):
            raise ControllerPhaseError("optional controller target is unsafe")
        os.unlink(target)
        if platform.system().lower()=="linux":
            directory=os.open(parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
            try:os.fsync(directory)
            finally:os.close(directory)


def install_target(source:Path,target:Path):
    raw,metadata=safe_private_file(source,"private controller input")
    atomic_file(target,raw,stat.S_IMODE(metadata.st_mode))


def safe_docker_socket(path:Path)->Path:
    requested=Path(os.path.abspath(path));resolved=requested.resolve(strict=True);metadata=os.lstat(resolved)
    safe_ancestors(resolved,"Docker socket")
    if not stat.S_ISSOCK(metadata.st_mode) or (platform.system().lower()=="linux" and metadata.st_uid!=0):
        raise ControllerPhaseError("Docker socket is unsafe")
    return resolved


def paths(args):
    return {"config":args.project_config,"env":args.project_env,"db":args.database,
            "wal":Path(str(args.database)+"-wal"),"shm":Path(str(args.database)+"-shm")}


def sqlite_digest(path:Path,*,immutable:bool=False)->str:
    requested=Path(os.path.abspath(path));metadata=os.lstat(requested)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1 \
            or (platform.system().lower()=="linux" and metadata.st_uid!=0):
        raise ControllerPhaseError("controller database metadata is unsafe")
    uri=f"file:{requested}?mode=ro"
    if immutable:uri+="&immutable=1"
    connection=sqlite3.connect(uri,uri=True,timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA integrity_check").fetchall()!= [("ok",)]:
            raise ControllerPhaseError("controller database integrity differs")
        digest=hashlib.sha256()
        for line in connection.iterdump():digest.update(line.encode("utf-8"));digest.update(b"\n")
        return digest.hexdigest()
    finally:connection.close()


def live_files_match_backup(args,backup:Path,manifest:dict)->None:
    target=paths(args)
    for key,name in (("orchestrator_config","config"),("orchestrator_env","env")):
        entry=manifest["artifacts"][key]
        if entry["present"]:
            expected,_=safe_file(backup/entry["filename"]);actual,_=safe_file(target[name])
            if actual!=expected:raise ControllerPhaseError("live controller file differs from backup baseline")
        elif os.path.lexists(target[name]):
            raise ControllerPhaseError("absent controller file appeared after backup")
    database=manifest["artifacts"]["orchestrator_database"]
    if database["present"]:
        if sqlite_digest(target["db"])!=sqlite_digest(backup/database["filename"],immutable=True):
            raise ControllerPhaseError("live controller database differs from backup baseline")
    elif any(os.path.lexists(target[name]) for name in ("db","wal","shm")):
        raise ControllerPhaseError("absent controller database appeared after backup")


def live_files_match_apply_inputs(args,backup:Path,manifest:dict)->None:
    """Bind apply inputs to the phase that is allowed to have created them.

    Upgrade transactions still require the live private files to equal their
    backup baseline.  During a clean install, however, the preceding
    clean-materials phase owns creation of config.yaml and .env.  Accept those
    two new files only when their bytes and modes exactly equal the sealed
    private inputs that this phase would install itself.  The controller
    database remains owned by this phase and must therefore still match the
    backup (including being absent on a clean target).
    """
    target=paths(args);clean=manifest.get("operation")=="clean-install"
    desired={"config":args.desired_config,"env":args.desired_env}
    for key,name in (("orchestrator_config","config"),("orchestrator_env","env")):
        entry=manifest["artifacts"][key]
        if entry["present"]:
            expected,_=safe_file(backup/entry["filename"]);actual,_=safe_file(target[name])
            if actual!=expected:raise ControllerPhaseError("live controller file differs from backup baseline")
        elif clean:
            if not os.path.lexists(target[name]):
                raise ControllerPhaseError("clean-materials controller file is absent")
            expected,expected_metadata=safe_private_file(desired[name],f"desired controller {name}")
            actual,actual_metadata=safe_file(target[name])
            if actual!=expected or stat.S_IMODE(actual_metadata.st_mode)!=stat.S_IMODE(expected_metadata.st_mode):
                raise ControllerPhaseError("clean-materials controller file differs from sealed input")
        elif os.path.lexists(target[name]):
            raise ControllerPhaseError("absent controller file appeared after backup")
    database=manifest["artifacts"]["orchestrator_database"]
    if database["present"]:
        if sqlite_digest(target["db"])!=sqlite_digest(backup/database["filename"],immutable=True):
            raise ControllerPhaseError("live controller database differs from backup baseline")
    elif any(os.path.lexists(target[name]) for name in ("db","wal","shm")):
        raise ControllerPhaseError("absent controller database appeared after backup")


def rollback(args,docker:Docker,backup:Path,manifest:dict,baseline:dict,release:str):
    hidden=f"noi-orchestrator-v1-old-{args.plan_id[:12]}";changed=False
    live=docker.inspect("noi-orchestrator",allow_absent=True)
    if live is not None and owned(live,args.plan_id,release):
        if (live.get("State") or {}).get("Running"):docker.stop(live["Id"]);wait_running(docker,"noi-orchestrator",False);changed=True
        docker.remove(live["Id"]);changed=True
    elif live is not None and not (baseline["present"] and inspect_matches(live,baseline)):
        raise ControllerPhaseError("canonical controller changed outside this transaction")
    elif live is not None and (live.get("State") or {}).get("Running"):
        # An uncertain quiesce/replace response can leave the exact baseline
        # process running.  Never overwrite its SQLite/config inputs first.
        docker.stop(live["Id"]);wait_running(docker,"noi-orchestrator",False);changed=True
    target=paths(args)
    restore_file(backup,manifest,"orchestrator_config",target["config"])
    restore_file(backup,manifest,"orchestrator_env",target["env"])
    restore_file(backup,manifest,"orchestrator_database",target["db"])
    restore_file(backup,manifest,"orchestrator_database_wal",target["wal"])
    restore_file(backup,manifest,"orchestrator_database_shm",target["shm"])
    old=docker.inspect(hidden,allow_absent=True)
    if baseline["present"]:
        if old is not None:
            if old.get("Id")!=baseline["container"]["container_id"]:raise ControllerPhaseError("retained controller identity differs")
            docker.rename(old["Id"],"noi-orchestrator");changed=True
        restored=docker.inspect("noi-orchestrator")
        if not inspect_matches(restored,baseline):raise ControllerPhaseError("restored controller identity differs")
        expected=baseline["container"]["running"]
        running=(restored.get("State") or {}).get("Running")
        # Never restart the old controller here.  Hydro, Caddy and source are
        # restored by later dependency phases.  The final rollback verifier
        # starts this exact retained container only after every dependency is
        # back at its sealed baseline.
        if running:docker.stop(restored["Id"]);wait_running(docker,"noi-orchestrator",False);changed=True
    elif old is not None:raise ControllerPhaseError("unexpected retained controller exists")
    return {"status":"rollback_verified","plan_id":args.plan_id,"backup_manifest_sha256":args.backup_manifest_sha256,
            "controller_present":baseline["present"],"baseline_running":bool(baseline["present"] and baseline["container"]["running"]),
            "controller_quiesced":True,"changed":changed,"other_container_mutations":0}


def apply(args,docker:Docker,backup:Path,manifest:dict,baseline:dict,desired:dict,release:str):
    hidden=f"noi-orchestrator-v1-old-{args.plan_id[:12]}"
    if docker.inspect(hidden,allow_absent=True) is not None:raise ControllerPhaseError("retained controller name is already occupied")
    live=docker.inspect("noi-orchestrator",allow_absent=True)
    if baseline["present"]:
        if not inspect_matches(live,baseline):raise ControllerPhaseError("live controller differs from backup baseline")
    elif live is not None:raise ControllerPhaseError("unexpected live controller exists")
    live_files_match_apply_inputs(args,backup,manifest)
    image=docker.image(desired["image_id"])
    if live is not None:
        if (live.get("State") or {}).get("Running"):docker.stop(live["Id"]);wait_running(docker,"noi-orchestrator",False)
        # The running baseline may have changed SQLite or its private inputs
        # after the first comparison.  Bind the quiesced bytes/semantics again
        # before the old container loses its canonical name.
        live_files_match_apply_inputs(args,backup,manifest)
        docker.rename(live["Id"],hidden)
    install_target(args.desired_config,args.project_config);install_target(args.desired_env,args.project_env)
    body=dict(desired["config"]);body["HostConfig"]=desired["host_config"]
    container_id=docker.create("noi-orchestrator",body);created=docker.inspect(container_id)
    if not owned(created,args.plan_id,release) or not created_matches_desired(created,desired,image):
        raise ControllerPhaseError("created controller identity differs")
    docker.start(container_id);running=wait_running(docker,"noi-orchestrator",True);local_health()
    return {"status":"verified","plan_id":args.plan_id,"container_id":running["Id"],
            "image_id":running["Image"],"healthy":True,"old_controller_retained":baseline["present"],
            "other_container_mutations":0}


def cleanup(args,docker:Docker,baseline:dict,release:str):
    live=docker.inspect("noi-orchestrator",allow_absent=True)
    if live is None or not owned(live,args.plan_id,release) or not (live.get("State") or {}).get("Running"):
        raise ControllerPhaseError("committed controller identity differs during cleanup")
    hidden=f"noi-orchestrator-v1-old-{args.plan_id[:12]}";old=docker.inspect(hidden,allow_absent=True)
    if old is not None:
        if not baseline["present"] or old.get("Id")!=baseline["container"]["container_id"] \
                or (old.get("State") or {}).get("Running"):
            raise ControllerPhaseError("retained controller differs during cleanup")
        docker.remove(old["Id"])
    return {"status":"cleanup_verified","plan_id":args.plan_id,"old_controller_removed":True,
            "other_container_mutations":0}


def main():
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply",action="store_true");mode.add_argument("--rollback",action="store_true");mode.add_argument("--commit-cleanup",action="store_true")
    parser.add_argument("--backup-directory",required=True,type=Path);parser.add_argument("--plan-id",required=True)
    parser.add_argument("--backup-manifest-sha256",required=True);parser.add_argument("--source-release",required=True)
    parser.add_argument("--docker-socket",type=Path,default=Path("/var/run/docker.sock"))
    parser.add_argument("--project-config",required=True,type=Path);parser.add_argument("--project-env",required=True,type=Path)
    parser.add_argument("--database",required=True,type=Path);parser.add_argument("--desired-definition",type=Path)
    parser.add_argument("--desired-config",type=Path);parser.add_argument("--desired-env",type=Path);args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0 or not HEX64.fullmatch(args.plan_id) \
                or not HEX64.fullmatch(args.backup_manifest_sha256) or not RELEASE.fullmatch(args.source_release):
            raise ControllerPhaseError("controller phase requires pinned Linux root")
        backup,manifest,baseline=backup_inputs(args.backup_directory,args.plan_id,args.backup_manifest_sha256)
        docker=Docker(safe_docker_socket(args.docker_socket))
        if args.apply:
            if None in (args.desired_definition,args.desired_config,args.desired_env):raise ControllerPhaseError("controller apply arguments differ")
            desired=desired_definition(args.desired_definition,args.plan_id,args.source_release)
            result=apply(args,docker,backup,manifest,baseline,desired,args.source_release)
        elif args.rollback:
            if any(value is not None for value in (args.desired_definition,args.desired_config,args.desired_env)):raise ControllerPhaseError("controller rollback arguments differ")
            result=rollback(args,docker,backup,manifest,baseline,args.source_release)
        else:
            if any(value is not None for value in (args.desired_definition,args.desired_config,args.desired_env)):raise ControllerPhaseError("controller cleanup arguments differ")
            result=cleanup(args,docker,baseline,args.source_release)
        print(json.dumps(result,sort_keys=True));return 0
    except (ControllerPhaseError,OSError,ValueError,BackupError,json.JSONDecodeError,sqlite3.Error) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
