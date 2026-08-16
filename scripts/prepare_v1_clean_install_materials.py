#!/usr/bin/env python3
"""Create or remove only the private files owned by one clean V1 install."""
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
import tempfile

from verify_v1_clean_install_backup import validate_clean_target
from verify_v1_install_backup import safe_directory, safe_file, validate_manifest


HEX64=re.compile(r"^[a-f0-9]{64}$")


class MaterialError(RuntimeError):pass


def canonical(value):return (json.dumps(value,sort_keys=True,separators=(",",":"))+"\n").encode()


def fsync_dir(path:Path):
    fd=os.open(path,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
    try:os.fsync(fd)
    finally:os.close(fd)


def atomic(path:Path,raw:bytes,mode:int):
    fd,temp=tempfile.mkstemp(prefix=f".{path.name}.v1-",dir=path.parent)
    try:
        if hasattr(os,"fchmod"):os.fchmod(fd,mode)
        else:os.chmod(temp,mode)
        with os.fdopen(fd,"wb",closefd=True) as out:fd=-1;out.write(raw);out.flush();os.fsync(out.fileno())
        os.replace(temp,path);fsync_dir(path.parent)
    finally:
        if fd>=0:os.close(fd)
        if os.path.lexists(temp):os.unlink(temp)


def private(path:Path,label:str,maximum=32*1024*1024)->bytes:
    requested=Path(os.path.abspath(path));resolved=requested.resolve(strict=True);info=os.lstat(requested)
    if requested!=resolved or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
        raise MaterialError(f"{label} metadata is unsafe")
    if platform.system().lower()=="linux" and (info.st_uid!=0 or stat.S_IMODE(info.st_mode)&0o077):
        raise MaterialError(f"{label} must be root-only")
    return safe_file(resolved,maximum=maximum)[0]


def inputs(args):
    backup=safe_directory(args.backup_directory);tx=safe_directory(args.transaction_directory)
    raw,_=safe_file(backup/"backup-manifest.json",maximum=4*1024*1024)
    if hashlib.sha256(raw).hexdigest()!=args.backup_manifest_sha256:raise MaterialError("backup manifest trust pin differs")
    manifest=validate_manifest(json.loads(raw.decode()),backup,expected_plan_id=args.plan_id)
    if manifest.get("operation")!="clean-install":raise MaterialError("materials phase requires a clean baseline")
    target=validate_clean_target(json.loads(safe_file(backup/"clean-target.json")[0].decode()))
    root=Path(os.path.abspath(args.install_root))
    expected={"install_root":root,"project_config":root/"orchestrator/config.yaml",
              "project_env":root/"orchestrator/.env","database":root/"orchestrator/data/orchestrator.db",
              "caddy_snippet":root/"orchestrator/runtime/caddy-exam.conf",
              "hydro_plugin_env":Path("/root/.hydro/orchestrator-plugin.env"),
              "hydro_plugin_token":Path("/root/.hydro/orchestrator-token")}
    if any(Path(target["paths"][key]["path"])!=path for key,path in expected.items()):
        raise MaterialError("clean target paths differ from materials phase")
    desired={"config":private(args.desired_config,"desired config"),
             "env":private(args.desired_env,"desired env"),
             "plugin_env":private(args.desired_plugin_env,"desired plugin env"),
             "token":private(args.desired_plugin_token,"desired plugin token",1024)}
    if not 32<=len(desired["token"].rstrip(b"\r\n"))<=512 or b"\x00" in desired["token"]:
        raise MaterialError("desired plugin token differs")
    return tx,root,desired,expected


def identity(args,desired):
    return {"schema_version":1,"operation":"prepare-clean-install-materials","plan_id":args.plan_id,
            "backup_manifest_sha256":args.backup_manifest_sha256,
            "files":{key:hashlib.sha256(raw).hexdigest() for key,raw in sorted(desired.items())},
            "status":"prepared"}


def load_exact(path:Path,expected:dict,label:str):
    try:value=json.loads(safe_file(path,maximum=1024*1024)[0].decode())
    except (OSError,UnicodeDecodeError,json.JSONDecodeError) as exc:raise MaterialError(f"{label} is unreadable") from exc
    if value!=expected:raise MaterialError(f"{label} differs")


def targets(root:Path):
    return {"config":root/"orchestrator/config.yaml","env":root/"orchestrator/.env",
            "plugin_env":Path("/root/.hydro/orchestrator-plugin.env"),
            "token":Path("/root/.hydro/orchestrator-token")}


def verify_files(root:Path,desired:dict):
    for key,path in targets(root).items():
        raw,info=safe_file(path)
        if raw!=desired[key] or (platform.system().lower()=="linux" and
                (info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600)):
            raise MaterialError("installed private material differs")


def safe_root(root:Path):
    requested=Path(os.path.abspath(root));resolved=requested.resolve(strict=True);info=os.lstat(requested)
    if requested!=resolved or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) \
            or (platform.system().lower()=="linux" and (info.st_uid!=0 or stat.S_IMODE(info.st_mode)&0o022)):
        raise MaterialError("install root is unsafe")
    return resolved


def apply(args):
    tx,root,desired,_=inputs(args);root=safe_root(root);pending=tx/f"clean-materials.{args.plan_id}.pending.json"
    committed=tx/f"clean-materials.{args.plan_id}.committed.json";row=identity(args,desired);done={**row,"status":"committed"}
    if os.path.lexists(committed):load_exact(committed,done,"materials commit receipt");verify_files(root,desired);return False
    if os.path.lexists(pending):raise MaterialError("unfinished materials phase requires rollback")
    for path in targets(root).values():
        if os.path.lexists(path):raise MaterialError("clean material target appeared before apply")
    atomic(pending,canonical(row),0o600)
    project=root/"orchestrator";data=project/"data";runtime=project/"runtime"
    for path,mode in ((project,0o700),(data,0o700),(runtime,0o700)):
        os.mkdir(path,mode);os.chown(path,0,0);os.chmod(path,mode);fsync_dir(path.parent)
    for key,path in targets(root).items():atomic(path,desired[key],0o600)
    verify_files(root,desired);atomic(committed,canonical(done),0o600);os.unlink(pending);fsync_dir(tx);return True


def rollback(args):
    tx,root,desired,_=inputs(args);row=identity(args,desired);pending=tx/f"clean-materials.{args.plan_id}.pending.json"
    committed=tx/f"clean-materials.{args.plan_id}.committed.json";receipt=tx/f"clean-materials.{args.plan_id}.rollback.json"
    terminal={**row,"status":"rollback_verified"}
    if os.path.lexists(receipt):load_exact(receipt,terminal,"materials rollback receipt");return False
    if os.path.lexists(pending):load_exact(pending,row,"materials pending receipt")
    elif os.path.lexists(committed):load_exact(committed,{**row,"status":"committed"},"materials commit receipt")
    changed=False
    for key,path in targets(root).items():
        if not os.path.lexists(path):continue
        if safe_file(path)[0]!=desired[key]:raise MaterialError("owned private material changed outside transaction")
        os.unlink(path);fsync_dir(path.parent);changed=True
    for path in (root/"orchestrator/runtime",root/"orchestrator/data",root/"orchestrator"):
        if not os.path.lexists(path):continue
        info=os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or any(path.iterdir()):
            raise MaterialError("owned clean install directory is not empty")
        os.rmdir(path);fsync_dir(path.parent);changed=True
    atomic(receipt,canonical(terminal),0o600)
    if os.path.lexists(pending):os.unlink(pending);fsync_dir(tx)
    return changed


def main():
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply",action="store_true");mode.add_argument("--rollback",action="store_true")
    parser.add_argument("--backup-directory",required=True,type=Path);parser.add_argument("--transaction-directory",required=True,type=Path)
    parser.add_argument("--plan-id",required=True);parser.add_argument("--backup-manifest-sha256",required=True)
    parser.add_argument("--install-root",required=True,type=Path);parser.add_argument("--desired-config",required=True,type=Path)
    parser.add_argument("--desired-env",required=True,type=Path);parser.add_argument("--desired-plugin-env",required=True,type=Path)
    parser.add_argument("--desired-plugin-token",required=True,type=Path);args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0 or not HEX64.fullmatch(args.plan_id) \
                or not HEX64.fullmatch(args.backup_manifest_sha256):raise MaterialError("materials phase requires pinned Linux root")
        changed=apply(args) if args.apply else rollback(args)
        print(json.dumps({"status":"verified" if args.apply else "rollback_verified","plan_id":args.plan_id,
                          "changed":changed,"service_mutations":0},sort_keys=True));return 0
    except (MaterialError,OSError,ValueError,UnicodeDecodeError,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
