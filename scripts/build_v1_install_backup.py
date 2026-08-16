#!/usr/bin/env python3
"""Collect and seal every live artifact required by one V1 upgrade."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import sqlite3
import stat
import sys
import tempfile
from urllib.parse import quote

from build_v1_cloud_install_backup import build as build_cloud
from build_v1_controller_install_backup import collect as collect_controller
from build_v1_hydro_install_backup import collect as collect_hydro
from build_v1_install_backup_manifest import build as seal_manifest
from build_v1_ordinary_oj_install_backup import build as build_ordinary
from commit_v1_caddy_config import Admin,adapt,canonical,get_live
from verify_v1_install_backup import safe_file


class CollectInstallError(RuntimeError):pass


def safe_ancestors(path:Path)->None:
    current=path.resolve(strict=True)
    while True:
        metadata=os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=0 \
                or stat.S_IMODE(metadata.st_mode)&0o022:
            raise CollectInstallError("install backup path ancestor is unsafe")
        if current.parent==current:return
        current=current.parent


def private_output(path:Path)->Path:
    requested=Path(os.path.abspath(path));parent=requested.parent.resolve(strict=True);metadata=os.lstat(parent)
    if os.path.lexists(requested):raise CollectInstallError("install backup output already exists")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=0 or stat.S_IMODE(metadata.st_mode)&0o022:
        raise CollectInstallError("install backup parent is unsafe")
    safe_ancestors(parent)
    requested.mkdir(mode=0o700);os.chown(requested,0,0);os.chmod(requested,0o700)
    descriptor=os.open(parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0));os.fsync(descriptor);os.close(descriptor)
    return requested.resolve(strict=True)


def atomic(path:Path,raw:bytes,mode:int=0o600)->None:
    if os.path.lexists(path):raise CollectInstallError(f"backup output already exists: {path.name}")
    descriptor,temporary=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent)
    try:
        if hasattr(os,"fchmod"):os.fchmod(descriptor,mode)
        else:os.chmod(temporary,mode)
        with os.fdopen(descriptor,"wb",closefd=True) as stream:
            descriptor=-1;stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        if platform.system().lower()=="linux":
            directory=os.open(path.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        if descriptor>=0:os.close(descriptor)
        if os.path.lexists(temporary):os.unlink(temporary)


def copy_file(source:Path,target:Path,*,optional=False)->bool:
    if not os.path.lexists(source):
        if optional:return False
        raise CollectInstallError(f"required live backup input is absent: {target.name}")
    safe_ancestors(Path(os.path.abspath(source)).parent)
    raw,metadata=safe_file(source);atomic(target,raw,stat.S_IMODE(metadata.st_mode));return True


def backup_database(source:Path,target:Path)->None:
    safe_ancestors(Path(os.path.abspath(source)).parent)
    metadata=os.lstat(source)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1 or metadata.st_uid!=0:
        raise CollectInstallError("live database metadata is unsafe")
    temporary=target.with_name(f".{target.name}.{os.urandom(8).hex()}")
    source_uri=quote(str(source.resolve(strict=True)),safe="/:")
    source_connection=sqlite3.connect(f"file:{source_uri}?mode=ro",uri=True,timeout=10)
    destination=None
    try:
        source_connection.execute("PRAGMA query_only=ON")
        if source_connection.execute("PRAGMA integrity_check").fetchall()!=[("ok",)]:raise CollectInstallError("live database integrity differs")
        destination=sqlite3.connect(temporary);source_connection.backup(destination);destination.commit()
        # A SQLite online backup copies the source database header, including
        # WAL journal mode.  Leaving the sealed copy in WAL mode means that a
        # later read-only integrity check can create zero-byte ``-wal`` and
        # ``-shm`` sidecars inside the already manifested backup directory.
        # The backup is a self-contained snapshot, so normalize its journal
        # mode before sealing it and require SQLite to confirm the transition.
        journal_mode=destination.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower()!="delete":
            raise CollectInstallError("sealed database journal mode differs")
        destination.commit();destination.close();destination=None
        os.chmod(temporary,stat.S_IMODE(metadata.st_mode))
        if platform.system().lower()=="linux":
            descriptor=os.open(temporary,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        os.replace(temporary,target)
        if platform.system().lower()=="linux":
            directory=os.open(target.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        if destination is not None:destination.close()
        source_connection.close()
        if os.path.lexists(temporary):os.unlink(temporary)


def source_pointer(path:Path,output:Path)->None:
    safe_ancestors(Path(os.path.abspath(path)).parent)
    metadata=os.lstat(path)
    if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid!=0:raise CollectInstallError("source pointer is unsafe")
    value=os.readlink(path)
    if not __import__("re").fullmatch(r"source-releases/[a-f0-9]{40}-[a-f0-9]{12}",value):raise CollectInstallError("source pointer differs")
    atomic(output,(value+"\n").encode())


def caddy(output:Path,caddyfile:Path,snippet:Path)->None:
    safe_ancestors(Path(os.path.abspath(caddyfile)).parent);safe_ancestors(Path(os.path.abspath(snippet)).parent)
    disk,metadata=safe_file(caddyfile,maximum=32*1024*1024);snippet_raw,snippet_metadata=safe_file(snippet,maximum=4*1024*1024)
    admin=Admin();live,_=get_live(admin)
    if canonical(adapt(admin,disk))!=canonical(live):raise CollectInstallError("Caddy disk and active config differ before backup")
    atomic(output/"Caddyfile",disk,stat.S_IMODE(metadata.st_mode));atomic(output/"caddy-exam.conf",snippet_raw,stat.S_IMODE(snippet_metadata.st_mode))
    atomic(output/"caddy-active.json",canonical(live)+b"\n")


def collect(args)->dict:
    root=private_output(args.output_directory)
    source_pointer(args.source_pointer,root/"source-pointer.txt")
    copy_file(args.project_config,root/"orchestrator-config.yaml");copy_file(args.project_env,root/"orchestrator.env")
    backup_database(args.database,root/"orchestrator.db")
    caddy(root,args.caddyfile,args.snippet)
    fixed={Path("/root/.hydro/addon.json"):"hydro-addon.json",Path("/root/.hydro/orchestrator-plugin.env"):"hydro-plugin.env",
           Path("/root/.hydro/orchestrator-token"):"hydro-plugin-token",Path("/root/.pm2/dump.pm2"):"pm2-dump.json"}
    for source,name in fixed.items():copy_file(source,root/name)
    copy_file(Path("/root/.pm2/dump.pm2.bak"),root/"pm2-dump.backup.json",optional=True)
    collect_hydro(root,args.pm2_bin);collect_controller(root,args.docker_socket,"noi-orchestrator")
    build_ordinary(root,args.oj_origin,args.pm2_bin);build_cloud(root)
    manifest=seal_manifest(root,args.plan_id,args.source_revision,args.candidate_manifest_sha256)
    raw,_=safe_file(root/"backup-manifest.json",maximum=4*1024*1024)
    return {"status":"sealed","plan_id":args.plan_id,"backup_manifest_sha256":__import__("hashlib").sha256(raw).hexdigest(),
            "artifacts":len(manifest["artifacts"]),"service_mutations":0}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--output-directory",required=True,type=Path);parser.add_argument("--plan-id",required=True)
    parser.add_argument("--source-revision",required=True);parser.add_argument("--candidate-manifest-sha256",required=True)
    parser.add_argument("--source-pointer",required=True,type=Path);parser.add_argument("--project-config",required=True,type=Path)
    parser.add_argument("--project-env",required=True,type=Path);parser.add_argument("--database",required=True,type=Path)
    parser.add_argument("--caddyfile",required=True,type=Path);parser.add_argument("--snippet",required=True,type=Path)
    parser.add_argument("--oj-origin",required=True);parser.add_argument("--pm2-bin",required=True,type=Path)
    parser.add_argument("--docker-socket",default=Path("/var/run/docker.sock"),type=Path);args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0:raise CollectInstallError("install backup collection requires Linux root")
        print(json.dumps(collect(args),sort_keys=True));return 0
    except (CollectInstallError,OSError,ValueError,sqlite3.Error,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
