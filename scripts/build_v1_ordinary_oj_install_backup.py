#!/usr/bin/env python3
"""Capture ordinary OJ health plus stable and restartable PM2 identities."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import sys
import tempfile

from collect_v1_ordinary_oj_observation import HTTPS_ORIGIN,pm2_rows,request
from verify_v1_install_backup import safe_directory
from verify_v1_ordinary_oj_install_backup import validate


class BuildOrdinaryError(RuntimeError):pass


def collect(origin:str,pm2:Path)->dict:
    home,_=request(origin,"/");login,_=request(origin,"/login");prep_status,prep=request(origin,"/prep/health",expect_json=True)
    value={"schema_version":1,"homepage_status":home,"login_status":login,
           "prep_health_ok":prep_status==200 and (prep or {}).get("ok") is True and (prep or {}).get("initialization")=="ready",
           "prep_database_ok":(prep or {}).get("database")=="ok","processes":pm2_rows(pm2)}
    return validate(value)


def build(output:Path,origin:str,pm2:Path)->dict:
    root=safe_directory(output);path=root/"ordinary-oj-before.json"
    if os.path.lexists(path):raise BuildOrdinaryError("ordinary OJ baseline output already exists")
    value=collect(origin,pm2);raw=(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n").encode()
    descriptor,temporary=tempfile.mkstemp(prefix=".ordinary-before.",dir=root)
    try:
        os.fchmod(descriptor,0o600)
        with os.fdopen(descriptor,"wb",closefd=True) as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path);directory=os.open(root,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.lexists(temporary):os.unlink(temporary)
    return {"status":"collected","stable_processes":3,"hydro_restart_allowed":True}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--output-directory",required=True,type=Path)
    parser.add_argument("--oj-origin",required=True);parser.add_argument("--pm2-bin",required=True,type=Path);args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0 or not HTTPS_ORIGIN.fullmatch(args.oj_origin):
            raise BuildOrdinaryError("ordinary OJ install baseline requires pinned Linux root")
        print(json.dumps(build(args.output_directory,args.oj_origin,args.pm2_bin),sort_keys=True));return 0
    except (BuildOrdinaryError,OSError,ValueError) as exc:print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
