#!/usr/bin/env python3
"""Restore the exact Hydro/PM2 subset from one sealed V1 install backup."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

from verify_v1_install_backup import BackupError, safe_directory, safe_file
from verify_v1_hydro_install_backup import (
    HydroBackupError, LAUNCH_KEYS, canonical, normalized_launch, verify as verify_backup,
    verify_pm2, verify_tree_archive,
)


ADDON_ROOT = Path("/root/.hydro/addons/orchestrator-submit")
STATE_ROOT = Path("/root/.hydro/orchestrator-state")
FIXED_FILES = {
    "hydro_addon_json": ("hydro-addon.json", Path("/root/.hydro/addon.json")),
    "hydro_plugin_env": ("hydro-plugin.env", Path("/root/.hydro/orchestrator-plugin.env")),
    "hydro_plugin_token": ("hydro-plugin-token", Path("/root/.hydro/orchestrator-token")),
    "pm2_dump": ("pm2-dump.json", Path("/root/.pm2/dump.pm2")),
}
OPTIONAL_FILES = {"pm2-dump.backup.json": Path("/root/.pm2/dump.pm2.bak")}
RENAME_EXCHANGE = 2
AT_FDCWD = -100


class RestoreError(RuntimeError):
    pass


def fsync_directory(path: Path) -> None:
    if platform.system().lower()!="linux": return
    descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def safe_parent(path: Path) -> Path:
    requested=Path(os.path.abspath(path)); resolved=requested.resolve(strict=True); metadata=os.lstat(requested)
    if requested!=resolved or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RestoreError(f"restore parent is unsafe: {path}")
    if platform.system().lower()=="linux" and (metadata.st_uid!=0 or stat.S_IMODE(metadata.st_mode)&0o022):
        raise RestoreError(f"restore parent is not root-only: {path}")
    return resolved


def remove_owned(path: Path, parent: Path) -> None:
    if not os.path.lexists(path): return
    if path.parent.resolve(strict=True)!=parent or not path.name.startswith("."):
        raise RestoreError("refusing to remove an unowned restore path")
    metadata=os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode): raise RestoreError("owned restore path is a symlink")
    if stat.S_ISDIR(metadata.st_mode): shutil.rmtree(path)
    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink==1: os.unlink(path)
    else: raise RestoreError("owned restore path metadata differs")
    fsync_directory(parent)


def exchange(left: Path, right: Path) -> None:
    libc=ctypes.CDLL(None,use_errno=True); renameat2=getattr(libc,"renameat2",None)
    if renameat2 is None: raise RestoreError("renameat2 is required for atomic tree restoration")
    renameat2.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    renameat2.restype=ctypes.c_int
    if renameat2(AT_FDCWD,os.fsencode(left),AT_FDCWD,os.fsencode(right),RENAME_EXCHANGE)!=0:
        error=ctypes.get_errno(); raise RestoreError(f"atomic tree exchange failed: errno={error}")
    fsync_directory(left.parent)


def archive_payload(raw: bytes, filename: str) -> tuple[dict, dict[str, bytes]]:
    state=verify_tree_archive(raw,filename); files={}
    with tarfile.open(fileobj=io.BytesIO(raw),mode="r:") as bundle:
        for entry in state["entries"]:
            if entry["type"]!="file": continue
            handle=bundle.extractfile("tree/"+entry["path"]); files[entry["path"]]=handle.read() if handle else b""
    return state,files


def tree_matches(target: Path, state: dict, files: dict[str,bytes]) -> bool:
    if not state["present"]: return not os.path.lexists(target)
    if not os.path.lexists(target): return False
    try:
        metadata=os.lstat(target)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode): return False
        if platform.system().lower()=="linux" and stat.S_IMODE(metadata.st_mode)!=state["root_mode"]: return False
        observed=[]
        for path in sorted(target.rglob("*"),key=lambda item:item.relative_to(target).as_posix()):
            relative=path.relative_to(target).as_posix(); info=os.lstat(path); mode=stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode): return False
            if stat.S_ISDIR(info.st_mode):
                observed.append({"path":relative,"type":"directory","mode":mode,"bytes":None,"sha256":None})
            elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
                content=path.read_bytes(); observed.append({"path":relative,"type":"file","mode":mode,
                    "bytes":len(content),"sha256":hashlib.sha256(content).hexdigest()})
                if files.get(relative)!=content: return False
            else: return False
        if platform.system().lower()=="linux": return observed==state["entries"]
        return [{**entry,"mode":0} for entry in observed] == [{**entry,"mode":0} for entry in state["entries"]]
    except OSError: return False


def create_stage(parent: Path, name: str, state: dict, files: dict[str,bytes]) -> Path:
    stage=parent/name
    if os.path.lexists(stage):
        if tree_matches(stage,state,files): return stage
        raise RestoreError("existing restore stage differs")
    os.mkdir(stage,state["root_mode"])
    try:
        for entry in state["entries"]:
            path=stage/PurePosixPath(entry["path"])
            if entry["type"]=="directory":
                path.mkdir(parents=True,exist_ok=False); os.chmod(path,entry["mode"])
        for entry in state["entries"]:
            if entry["type"]!="file": continue
            path=stage/PurePosixPath(entry["path"]); path.parent.mkdir(parents=True,exist_ok=True)
            descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,"O_NOFOLLOW",0),entry["mode"])
            with os.fdopen(descriptor,"wb",closefd=True) as output:
                output.write(files[entry["path"]]); output.flush(); os.fsync(output.fileno())
        for directory in sorted((p for p in stage.rglob("*") if p.is_dir()),key=lambda p:len(p.parts),reverse=True):
            fsync_directory(directory)
        os.chmod(stage,state["root_mode"]); fsync_directory(stage); fsync_directory(parent)
        return stage
    except BaseException:
        # Keep a partial root-only stage as crash evidence; a rerun refuses it.
        raise


def restore_tree(target: Path, raw: bytes, filename: str, plan_id: str) -> None:
    state,files=archive_payload(raw,filename); parent=safe_parent(target.parent)
    stage=parent/f".{target.name}.v1-restore-{plan_id[:12]}"
    displaced=parent/f".{target.name}.v1-displaced-{plan_id[:12]}"
    if tree_matches(target,state,files):
        remove_owned(stage,parent); remove_owned(displaced,parent); return
    if os.path.lexists(displaced): raise RestoreError("unfinished absent-tree restoration is ambiguous")
    if not state["present"]:
        if not os.path.lexists(target): return
        info=os.lstat(target)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RestoreError("live Hydro tree metadata is unsafe")
        os.replace(target,displaced); fsync_directory(parent)
        if not tree_matches(target,state,files): raise RestoreError("absent Hydro tree restoration failed")
        remove_owned(displaced,parent); return
    stage=create_stage(parent,stage.name,state,files)
    if os.path.lexists(target):
        info=os.lstat(target)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RestoreError("live Hydro tree metadata is unsafe")
        exchange(target,stage)
    else:
        os.replace(stage,target); fsync_directory(parent)
    if not tree_matches(target,state,files): raise RestoreError("Hydro tree restoration differs")
    remove_owned(stage,parent)


def atomic_restore_file(source: Path, target: Path) -> None:
    raw,metadata=safe_file(source); parent=safe_parent(target.parent)
    descriptor,temporary=tempfile.mkstemp(prefix=f".{target.name}.v1-restore.",dir=parent)
    try:
        os.fchmod(descriptor,stat.S_IMODE(metadata.st_mode))
        with os.fdopen(descriptor,"wb",closefd=True) as output:
            descriptor=-1; output.write(raw); output.flush(); os.fsync(output.fileno())
        os.replace(temporary,target); fsync_directory(parent)
    finally:
        if descriptor>=0: os.close(descriptor)
        if os.path.lexists(temporary): os.unlink(temporary)


def restore_optional_file(source: Path, target: Path, present: bool, plan_id: str) -> None:
    if present: atomic_restore_file(source,target); return
    parent=safe_parent(target.parent); displaced=parent/f".{target.name}.v1-displaced-{plan_id[:12]}"
    if not os.path.lexists(target):
        remove_owned(displaced,parent); return
    if os.path.lexists(displaced): raise RestoreError("unfinished optional-file restoration is ambiguous")
    info=os.lstat(target)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
        raise RestoreError("optional live file metadata is unsafe")
    os.replace(target,displaced); fsync_directory(parent); remove_owned(displaced,parent)


def run_pm2(pm2: Path, arguments: list[str], *, dump: Path|None=None, allow_missing=False) -> None:
    requested=Path(os.path.abspath(pm2)); resolved=requested.resolve(strict=True); metadata=os.lstat(requested)
    if requested!=resolved or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) \
            or metadata.st_nlink!=1 or not os.access(resolved,os.X_OK):
        raise RestoreError("PM2 restore executable is unsafe")
    if platform.system().lower()=="linux" and (metadata.st_uid!=0 or stat.S_IMODE(metadata.st_mode)&0o022):
        raise RestoreError("PM2 restore executable is not trusted")
    environment={"HOME":"/root","USER":"root","LOGNAME":"root","SHELL":"/bin/bash","PM2_HOME":"/root/.pm2",
        "PATH":"/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:/usr/local/sbin:/usr/local/bin:"
               "/usr/sbin:/usr/bin:/sbin:/bin","LANG":"C.UTF-8","LC_ALL":"C.UTF-8"}
    if dump is not None:
        environment["PM2_DUMP_FILE_PATH"]=str(dump)
        environment["PM2_DUMP_BACKUP_FILE_PATH"]=str(dump.with_name("no-fallback.dump"))
    completed=subprocess.run([str(resolved),*arguments],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,env=environment,timeout=60,check=False)
    if completed.returncode!=0 and not allow_missing: raise RestoreError("PM2 Hydro restore command failed")


def filtered_dump(backup: Path, transaction: Path) -> Path:
    raw,_=safe_file(backup/"pm2-dump.json")
    rows=json.loads(raw.decode("utf-8")); hydro=[row for row in rows if isinstance(row,dict) and row.get("name")=="hydrooj"]
    if len(hydro)!=1: raise RestoreError("PM2 backup does not contain one Hydro definition")
    path=transaction/"hydro-only-baseline.dump.pm2"; content=(json.dumps(hydro,separators=(",",":"),ensure_ascii=False)+"\n").encode()
    if os.path.lexists(path):
        existing,_=safe_file(path)
        if existing!=content: raise RestoreError("filtered Hydro dump differs")
    else: atomic_bytes_local(path,content)
    return path


def atomic_bytes_local(path: Path, raw: bytes) -> None:
    descriptor,temporary=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent)
    try:
        os.fchmod(descriptor,0o600)
        with os.fdopen(descriptor,"wb",closefd=True) as output:
            descriptor=-1; output.write(raw); output.flush(); os.fsync(output.fileno())
        os.replace(temporary,path); fsync_directory(path.parent)
    finally:
        if descriptor>=0: os.close(descriptor)
        if os.path.lexists(temporary): os.unlink(temporary)


def wait_hydro(seconds: int=120) -> None:
    import urllib.request
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8888/",timeout=3) as response:
                if response.status==200: return
        except Exception: pass
        time.sleep(1)
    raise RestoreError("Hydro did not become healthy after restoration")


def verify_live_pm2(pm2: Path, backup: Path) -> None:
    from build_v1_hydro_install_backup import pm2_jlist
    live=pm2_jlist(pm2); rows=[row for row in live if isinstance(row,dict) and row.get("name")=="hydrooj"]
    if len(rows)!=1 or not isinstance(rows[0].get("pm2_env"),dict): raise RestoreError("live Hydro PM2 definition is absent")
    actual=rows[0]["pm2_env"]; definition=json.loads((backup/"hydro-pm2-definition.json").read_text())
    env=actual.get("env"); normalized=dict(env) if isinstance(env,dict) else {}; normalized.pop("unique_id",None)
    prefix={k:v for k,v in (env or {}).items() if k.startswith("ORCHESTRATOR_")}
    top={k:v for k,v in actual.items() if k.startswith("ORCHESTRATOR_")}
    if hashlib.sha256(canonical(normalized)).hexdigest()!=definition["normalized_env_sha256"] \
            or hashlib.sha256(canonical(prefix)).hexdigest()!=definition["orchestrator_prefix_sha256"] \
            or hashlib.sha256(canonical(top)).hexdigest()!=definition["top_orchestrator_prefix_sha256"] \
            or normalized_launch(actual)!=definition["launch"]:
        raise RestoreError("live Hydro PM2 definition differs from backup")


def live_matches_backup(backup: Path, pm2: Path, manifest: dict) -> bool:
    try:
        for key,(source,target) in FIXED_FILES.items():
            entry=manifest["artifacts"][key]
            if entry["present"]:
                expected,_=safe_file(backup/source); actual,_=safe_file(target)
                if expected!=actual: return False
            elif os.path.lexists(target): return False
        optional=manifest["artifacts"]["pm2_dump_backup"]
        target=OPTIONAL_FILES["pm2-dump.backup.json"]
        if optional["present"]:
            expected,_=safe_file(backup/"pm2-dump.backup.json"); actual,_=safe_file(target)
            if expected!=actual: return False
        elif os.path.lexists(target): return False
        addon_raw,_=safe_file(backup/"hydro-addon-tree.tar")
        state_raw,_=safe_file(backup/"hydro-plugin-state.tar")
        if not tree_matches(ADDON_ROOT,*archive_payload(addon_raw,"hydro-addon-tree.tar")) \
                or not tree_matches(STATE_ROOT,*archive_payload(state_raw,"hydro-plugin-state.tar")):
            return False
        verify_live_pm2(pm2,backup)
        return True
    except (OSError,BackupError,RestoreError,ValueError):
        return False


def restore(backup: Path, transaction: Path, plan_id: str, manifest_sha256: str, pm2: Path) -> dict:
    root=safe_directory(backup); tx=safe_directory(transaction)
    manifest_raw,_=safe_file(root/"backup-manifest.json",maximum=4*1024*1024)
    if hashlib.sha256(manifest_raw).hexdigest()!=manifest_sha256: raise RestoreError("backup manifest trust pin differs")
    verify_backup(root,plan_id)
    manifest=json.loads(manifest_raw.decode("utf-8"))
    if live_matches_backup(root,pm2,manifest):
        return {"status":"rollback_verified","plan_id":plan_id,"backup_manifest_sha256":manifest_sha256,
                "hydro":"online","other_pm2_mutations":0,"changed":False}
    # Stop only Hydro.  A missing Hydro process is acceptable for an uncertain
    # failed apply; every other PM2 application remains untouched.
    run_pm2(pm2,["delete","hydrooj"],allow_missing=True)
    from build_v1_hydro_install_backup import pm2_jlist
    if any(isinstance(row,dict) and row.get("name")=="hydrooj" for row in pm2_jlist(pm2)):
        raise RestoreError("PM2 Hydro process still exists after delete")
    for key,(source,target) in FIXED_FILES.items():
        if key=="pm2_dump": continue
        entry=manifest["artifacts"][key]
        restore_optional_file(root/source,target,entry["present"],plan_id)
    addon_raw,_=safe_file(root/"hydro-addon-tree.tar"); state_raw,_=safe_file(root/"hydro-plugin-state.tar")
    restore_tree(ADDON_ROOT,addon_raw,"hydro-addon-tree.tar",plan_id)
    restore_tree(STATE_ROOT,state_raw,"hydro-plugin-state.tar",plan_id)
    dump=filtered_dump(root,tx); run_pm2(pm2,["resurrect"],dump=dump); wait_hydro()
    atomic_restore_file(root/"pm2-dump.json",FIXED_FILES["pm2_dump"][1])
    optional=manifest["artifacts"]["pm2_dump_backup"]
    restore_optional_file(root/"pm2-dump.backup.json",OPTIONAL_FILES["pm2-dump.backup.json"],optional["present"],plan_id)
    verify_live_pm2(pm2,root)
    for key,(source,target) in FIXED_FILES.items():
        entry=manifest["artifacts"][key]
        if entry["present"]:
            expected,_=safe_file(root/source); actual,_=safe_file(target)
            if expected!=actual: raise RestoreError(f"restored Hydro file differs: {source}")
        elif os.path.lexists(target): raise RestoreError(f"absent Hydro file was not removed: {source}")
    if not tree_matches(ADDON_ROOT,*archive_payload(addon_raw,"hydro-addon-tree.tar")) \
            or not tree_matches(STATE_ROOT,*archive_payload(state_raw,"hydro-plugin-state.tar")):
        raise RestoreError("restored Hydro tree differs")
    return {"status":"rollback_verified","plan_id":plan_id,"backup_manifest_sha256":manifest_sha256,
            "hydro":"online","other_pm2_mutations":0,"changed":True}


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--backup-directory",type=Path,required=True)
    parser.add_argument("--transaction-directory",type=Path,required=True); parser.add_argument("--plan-id",required=True)
    parser.add_argument("--backup-manifest-sha256",required=True); parser.add_argument("--pm2-bin",type=Path,required=True)
    args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0:
            raise RestoreError("Hydro install restoration requires Linux root")
        print(json.dumps(restore(args.backup_directory,args.transaction_directory,args.plan_id,
                                 args.backup_manifest_sha256,args.pm2_bin),sort_keys=True)); return 0
    except (RestoreError,HydroBackupError,BackupError,OSError,ValueError,subprocess.SubprocessError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr); return 2


if __name__=="__main__": raise SystemExit(main())
