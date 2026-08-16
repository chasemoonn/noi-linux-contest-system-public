#!/usr/bin/env python3
"""Collect the exact existing NOI controller identity without mutating Docker."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import socket
import stat
import sys
import tempfile
from urllib.parse import quote

from verify_v1_controller_install_backup import canonical, validate_definition, validate_image
from verify_v1_install_backup import safe_directory


class CollectError(RuntimeError): pass


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path): super().__init__("localhost",timeout=10); self.path=str(path)
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); self.sock.settimeout(self.timeout); self.sock.connect(self.path)


def docker_get(socket_path: Path, path: str, *, allow_absent=False):
    connection=UnixHTTPConnection(socket_path)
    try:
        connection.request("GET",path); response=connection.getresponse(); raw=response.read(16*1024*1024+1)
    except (OSError,http.client.HTTPException) as exc: raise CollectError("controller Docker inspection failed") from exc
    finally: connection.close()
    if allow_absent and response.status==404: return None
    if response.status!=200 or len(raw)>16*1024*1024: raise CollectError("controller Docker inspection response differs")
    try: value=json.loads(raw)
    except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise CollectError("controller Docker inspection is invalid JSON") from exc
    if not isinstance(value,dict): raise CollectError("controller Docker inspection is not an object")
    return value


def safe_docker_socket(path: Path) -> Path:
    requested=Path(os.path.abspath(path)); resolved=requested.resolve(strict=True); metadata=os.lstat(resolved)
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid!=0:
        raise CollectError("controller Docker socket is unsafe")
    current=resolved.parent
    while True:
        info=os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 \
                or stat.S_IMODE(info.st_mode)&0o022:
            raise CollectError("controller Docker socket ancestor is unsafe")
        if current.parent==current:break
        current=current.parent
    return resolved


def immutable_identity(value: dict) -> dict:
    host=value.get("HostConfig") or {}; config=value.get("Config") or {}
    mounts=sorted(({"type":x.get("Type"),"source":x.get("Source"),"destination":x.get("Destination"),
                    "rw":x.get("RW"),"propagation":x.get("Propagation")} for x in value.get("Mounts") or []),
                  key=canonical)
    if not isinstance(config,dict) or not isinstance(host,dict):
        raise CollectError("controller Docker definition differs")
    return {"container_id":value.get("Id"),"name":value.get("Name"),"image_id":value.get("Image"),
            "config":config,"host_config":host,"mounts":mounts}


def atomic_json(path: Path, value: dict):
    if os.path.lexists(path): raise CollectError("controller backup output already exists")
    descriptor,temporary=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent)
    try:
        os.fchmod(descriptor,0o600)
        with os.fdopen(descriptor,"wb",closefd=True) as output:
            output.write(canonical(value)+b"\n"); output.flush(); os.fsync(output.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.lexists(temporary): os.unlink(temporary)


def collect(output: Path, docker_socket: Path, name: str) -> dict:
    root=safe_directory(output); socket_path=safe_docker_socket(docker_socket)
    value=docker_get(socket_path,f"/containers/{quote(name)}/json",allow_absent=True)
    if value is None:
        definition={"schema_version":1,"present":False,"container":None}
        image={"schema_version":1,"present":False,"image_id":None}
    else:
        image=docker_get(socket_path,f"/images/{quote(str(value.get('Image')),safe='')}/json")
        if image.get("Id")!=value.get("Image"):
            raise CollectError("controller image identity differs")
        state=value.get("State") or {}; identity=immutable_identity(value)
        definition={"schema_version":1,"present":True,"container":{
            "container_id":value.get("Id"),"name":value.get("Name"),"image_id":value.get("Image"),
            "running":state.get("Running"),"restart_count":value.get("RestartCount"),
            "immutable_identity":identity,
            "immutable_identity_sha256":hashlib.sha256(canonical(identity)).hexdigest()}}
        image_row={"schema_version":1,"present":True,"image_id":value.get("Image")}
    if value is None:image_row=image
    validate_definition(definition); validate_image(image_row,definition)
    atomic_json(root/"controller-definition.json",definition); atomic_json(root/"controller-image.json",image_row)
    return {"status":"collected","controller_present":definition["present"],"image_id":image_row["image_id"]}


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--output-directory",required=True,type=Path)
    parser.add_argument("--docker-socket",type=Path,default=Path("/var/run/docker.sock"))
    parser.add_argument("--container-name",default="noi-orchestrator"); args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0 or args.container_name!="noi-orchestrator":
            raise CollectError("controller backup collection requires pinned Linux root")
        print(json.dumps(collect(args.output_directory,args.docker_socket,args.container_name),sort_keys=True)); return 0
    except (CollectError,OSError,ValueError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr); return 2


if __name__=="__main__": raise SystemExit(main())
