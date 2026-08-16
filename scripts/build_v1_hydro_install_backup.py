#!/usr/bin/env python3
"""Collect deterministic Hydro tree and PM2 semantics into an install backup."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile

from verify_v1_install_backup import BackupError, safe_directory, safe_file
from verify_v1_hydro_install_backup import (
    LAUNCH_KEYS, canonical, normalized_launch, verify_pm2, verify_tree_archive,
)


ADDON_ROOT = Path("/root/.hydro/addons/orchestrator-submit")
STATE_ROOT = Path("/root/.hydro/orchestrator-state")


class CollectError(RuntimeError):
    pass


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def atomic_bytes(path: Path, raw: bytes, mode: int = 0o600) -> None:
    if os.path.lexists(path):
        raise CollectError(f"backup artifact already exists: {path.name}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1; output.write(raw); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path); fsync_directory(path.parent)
    finally:
        if descriptor >= 0: os.close(descriptor)
        if os.path.lexists(temporary): os.unlink(temporary)


def safe_tree(root: Path) -> tuple[bool, int | None, list[dict], dict[str, bytes]]:
    if not os.path.lexists(root):
        return False, None, [], {}
    requested = Path(os.path.abspath(root)); resolved = requested.resolve(strict=True); metadata = os.lstat(requested)
    if requested != resolved or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CollectError(f"Hydro backup tree is unsafe: {root}")
    if platform.system().lower() == "linux" and metadata.st_uid != 0:
        raise CollectError(f"Hydro backup tree is not root-owned: {root}")
    entries=[]; files={}
    for path in sorted(root.rglob("*"), key=lambda item:item.relative_to(root).as_posix()):
        relative=path.relative_to(root).as_posix(); info=os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise CollectError(f"Hydro backup tree contains a symlink: {relative}")
        mode=stat.S_IMODE(info.st_mode)
        if platform.system().lower() == "linux" and info.st_uid != 0:
            raise CollectError(f"Hydro backup tree entry is not root-owned: {relative}")
        if stat.S_ISDIR(info.st_mode):
            entries.append({"path":relative,"type":"directory","mode":mode,"bytes":None,"sha256":None})
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 <= info.st_size <= 256*1024*1024:
            raise CollectError(f"Hydro backup tree entry metadata is unsafe: {relative}")
        descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
        try:
            opened=os.fstat(descriptor); content=os.read(descriptor,opened.st_size+1)
            if len(content)!=opened.st_size or (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino):
                raise CollectError(f"Hydro backup tree entry changed while reading: {relative}")
        finally: os.close(descriptor)
        entries.append({"path":relative,"type":"file","mode":mode,"bytes":len(content),
                        "sha256":hashlib.sha256(content).hexdigest()}); files[relative]=content
    # A present empty directory is legitimate and distinct from absence.
    return True, stat.S_IMODE(metadata.st_mode), entries, files


def build_tree_archive(root: Path) -> bytes:
    present,root_mode,entries,files=safe_tree(root)
    state={"schema_version":1,"root":str(root),"present":present,
           "root_mode":root_mode,"entries":entries}
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode="w:",format=tarfile.USTAR_FORMAT) as bundle:
        state_raw=(json.dumps(state,sort_keys=True,separators=(",",":"),ensure_ascii=False)+"\n").encode()
        info=tarfile.TarInfo("tree-state.json"); info.size=len(state_raw); info.mode=0o600; info.uid=0; info.gid=0
        info.mtime=0; bundle.addfile(info,io.BytesIO(state_raw))
        for entry in entries:
            if entry["type"] != "file": continue
            content=files[entry["path"]]; info=tarfile.TarInfo("tree/"+entry["path"])
            info.size=len(content); info.mode=entry["mode"]; info.uid=0; info.gid=0; info.mtime=0
            bundle.addfile(info,io.BytesIO(content))
    raw=output.getvalue()
    filename="hydro-addon-tree.tar" if root==ADDON_ROOT else "hydro-plugin-state.tar"
    verify_tree_archive(raw,filename)
    return raw


def pm2_jlist(pm2: Path) -> list:
    requested=Path(os.path.abspath(pm2)); resolved=requested.resolve(strict=True); metadata=os.lstat(requested)
    if requested!=resolved or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) \
            or not os.access(resolved,os.X_OK):
        raise CollectError("PM2 executable is unsafe")
    environment={"HOME":"/root","USER":"root","LOGNAME":"root","SHELL":"/bin/bash",
                 "PM2_HOME":"/root/.pm2","PATH":"/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:"
                 "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                 "LANG":"C.UTF-8","LC_ALL":"C.UTF-8"}
    try: completed=subprocess.run([str(resolved),"jlist","--silent"],stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment,
                                  timeout=30,check=False)
    except (OSError,subprocess.TimeoutExpired) as exc: raise CollectError("PM2 jlist did not complete") from exc
    if completed.returncode!=0 or not 0<len(completed.stdout)<=64*1024*1024:
        raise CollectError("PM2 jlist failed")
    try: value=json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise CollectError("PM2 jlist output is invalid") from exc
    if not isinstance(value,list): raise CollectError("PM2 jlist output differs")
    return value


def pm2_definition(dump_raw: bytes, live: list) -> bytes:
    try: dump=json.loads(dump_raw.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise CollectError("PM2 dump is invalid") from exc
    dump_rows=[row for row in dump if isinstance(row,dict) and row.get("name")=="hydrooj"] if isinstance(dump,list) else []
    live_rows=[row for row in live if isinstance(row,dict) and row.get("name")=="hydrooj"]
    if len(dump_rows)!=1 or len(live_rows)!=1 or not isinstance(live_rows[0].get("pm2_env"),dict):
        raise CollectError("PM2 must contain one persistent and one live Hydro definition")
    persistent=dump_rows[0]; actual=live_rows[0]["pm2_env"]
    persistent_env=persistent.get("env"); actual_env=actual.get("env")
    if not isinstance(persistent_env,dict) or not isinstance(actual_env,dict):
        raise CollectError("PM2 live Hydro environment differs from persistent dump")
    normalized=dict(persistent_env); normalized.pop("unique_id",None)
    normalized_actual=dict(actual_env); normalized_actual.pop("unique_id",None)
    if normalized!=normalized_actual:
        raise CollectError("PM2 live Hydro environment differs from persistent dump")
    if {k:v for k,v in persistent.items() if k.startswith("ORCHESTRATOR_")} != \
            {k:v for k,v in actual.items() if k.startswith("ORCHESTRATOR_")}:
        raise CollectError("PM2 live Hydro top-level environment differs from persistent dump")
    if normalized_launch(persistent) != normalized_launch(actual):
        raise CollectError("PM2 live Hydro launch definition differs from persistent dump")
    prefix={k:v for k,v in persistent_env.items() if k.startswith("ORCHESTRATOR_")}
    top={k:v for k,v in persistent.items() if k.startswith("ORCHESTRATOR_")}
    definition={"schema_version":1,"name":"hydrooj",
                "dump_row_sha256":hashlib.sha256(canonical(persistent)).hexdigest(),
                "normalized_env_sha256":hashlib.sha256(canonical(normalized)).hexdigest(),
                "orchestrator_prefix_sha256":hashlib.sha256(canonical(prefix)).hexdigest(),
                "top_orchestrator_prefix_sha256":hashlib.sha256(canonical(top)).hexdigest(),
                "launch":normalized_launch(persistent)}
    raw=(json.dumps(definition,sort_keys=True,separators=(",",":"),ensure_ascii=False)+"\n").encode()
    verify_pm2(dump_raw,raw); return raw


def collect(backup: Path, pm2: Path) -> dict:
    root=safe_directory(backup)
    if os.path.lexists(root/"backup-manifest.json"):
        raise CollectError("Hydro semantic artifacts must be collected before sealing")
    dump_raw,_=safe_file(root/"pm2-dump.json")
    outputs={"hydro-addon-tree.tar":build_tree_archive(ADDON_ROOT),
             "hydro-plugin-state.tar":build_tree_archive(STATE_ROOT),
             "hydro-pm2-definition.json":pm2_definition(dump_raw,pm2_jlist(pm2))}
    if any(os.path.lexists(root/name) for name in outputs):
        raise CollectError("Hydro semantic backup artifact already exists")
    for name,raw in outputs.items(): atomic_bytes(root/name,raw)
    return {"status":"collected","artifacts":sorted(outputs),
            "sha256":{name:hashlib.sha256(raw).hexdigest() for name,raw in outputs.items()}}


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("backup_directory",type=Path)
    parser.add_argument("--pm2-bin",required=True,type=Path); args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0:
            raise CollectError("Hydro install backup collection requires Linux root")
        print(json.dumps(collect(args.backup_directory,args.pm2_bin),sort_keys=True)); return 0
    except (CollectError,BackupError,OSError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr); return 2


if __name__=="__main__": raise SystemExit(main())
