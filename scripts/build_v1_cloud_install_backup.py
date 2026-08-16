#!/usr/bin/env python3
"""Capture a closed cloud baseline from an existing healthy controller."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time

from verify_v1_cloud_install_backup import validate
from verify_v1_install_backup import safe_directory


class BuildCloudError(RuntimeError):pass


def health()->dict:
    connection=http.client.HTTPConnection("127.0.0.1",8600,timeout=10)
    try:
        connection.request("GET","/healthz",headers={"Accept":"application/json"});response=connection.getresponse();raw=response.read(2*1024*1024+1)
    except (OSError,http.client.HTTPException) as exc:raise BuildCloudError("controller health probe failed") from exc
    finally:connection.close()
    try:value=json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise BuildCloudError("controller health is invalid JSON") from exc
    if response.status!=200 or not isinstance(value,dict) or value.get("ok") is not True:
        raise BuildCloudError("controller health differs")
    desktop=value.get("desktop_access")
    if not isinstance(desktop,dict):raise BuildCloudError("controller desktop health is absent")
    return desktop


def stable_health()->dict:
    rows=[]
    for index in range(3):
        rows.append(health())
        if index<2:time.sleep(1)
    selected=("enabled","desired_open","open","closed","healthy","managed_count","conflict_count",
              "management_healthy","management_missing_count","instance_state")
    normalized=[{key:row.get(key) for key in selected} for row in rows]
    if normalized[1:]!=normalized[:-1]:raise BuildCloudError("cloud baseline did not stabilize")
    return rows[-1]


def build(output:Path,desktop:dict|None=None)->dict:
    root=safe_directory(output);path=root/"cloud-before.json"
    if os.path.lexists(path):raise BuildCloudError("cloud baseline output already exists")
    source=desktop if desktop is not None else stable_health()
    keys=("enabled","desired_open","open","closed","healthy","managed_count","conflict_count","management_healthy","management_missing_count","instance_state")
    value=validate({"schema_version":1,**{key:source.get(key) for key in keys}})
    raw=(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n").encode();descriptor,temporary=tempfile.mkstemp(prefix=".cloud-before.",dir=root)
    try:
        if hasattr(os,"fchmod"):os.fchmod(descriptor,0o600)
        else:os.chmod(temporary,0o600)
        with os.fdopen(descriptor,"wb",closefd=True) as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        descriptor=-1;os.replace(temporary,path)
        if platform.system().lower()=="linux":
            directory=os.open(root,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        if descriptor>=0:os.close(descriptor)
        if os.path.lexists(temporary):os.unlink(temporary)
    return {"status":"collected","closed":True,"instance_state":"STOPPED"}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--output-directory",required=True,type=Path);args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0:raise BuildCloudError("cloud backup collection requires Linux root")
        print(json.dumps(build(args.output_directory),sort_keys=True));return 0
    except (BuildCloudError,OSError,ValueError) as exc:print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
