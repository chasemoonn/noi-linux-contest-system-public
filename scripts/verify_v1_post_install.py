#!/usr/bin/env python3
"""Read-only hard gate for the terminal V1 install phase."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import sqlite3
import ssl
import stat
import sys
import time
import urllib.error
import urllib.request

from apply_v1_controller import Docker, owned, safe_docker_socket, safe_private_file
from commit_v1_caddy_config import Admin, adapt, canonical as caddy_canonical, get_live
from build_v1_ordinary_oj_install_backup import collect as collect_ordinary
from collect_v1_ordinary_oj_observation import ObservationError
from verify_v1_install_backup import safe_directory, safe_file, validate_manifest
from verify_v1_ordinary_oj_install_backup import compare as compare_ordinary,validate as validate_ordinary


HEX64=re.compile(r"^[a-f0-9]{64}$"); RELEASE=re.compile(r"^[a-f0-9]{40}-[a-f0-9]{12}$")
IMAGE=re.compile(r"^sha256:[a-f0-9]{64}$"); ORIGIN=re.compile(r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]+)?$")
UNSAFE_QUEUE={"pending","retry","sending","ambiguous","permanent_failed"}
ACTIVE_CONTEST={"registered","preparing","ready","collecting","safe_wait"}


class PostInstallError(RuntimeError):pass


def exact(value,keys,label):
    if not isinstance(value,dict) or set(value)!=set(keys):raise PostInstallError(f"{label} field set differs")
    return value


def contract(path:Path,plan_id:str,source_release:str)->dict:
    raw,_=safe_private_file(path,"post-install contract",maximum=1024*1024)
    try:value=json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise PostInstallError("post-install contract is invalid") from exc
    row=exact(value,{"schema_version","plan_id","source_release","controller_image_id","oj_origin","exam_origin",
                     "source_pointer","caddyfile","snippet","project_config","project_env","database","pm2_bin","docker_socket"},"post-install contract")
    if row["schema_version"]!=1 or row["plan_id"]!=plan_id or row["source_release"]!=source_release \
            or not IMAGE.fullmatch(str(row["controller_image_id"])) \
            or not ORIGIN.fullmatch(str(row["oj_origin"])) or not ORIGIN.fullmatch(str(row["exam_origin"])) \
            or row["oj_origin"]==row["exam_origin"]:
        raise PostInstallError("post-install contract identity differs")
    for name in ("source_pointer","caddyfile","snippet","project_config","project_env","database","pm2_bin","docker_socket"):
        if not isinstance(row[name],str) or not PurePosixPath(row[name]).is_absolute() or "\x00" in row[name]:
            raise PostInstallError(f"post-install contract path differs: {name}")
    return row


def request(origin:str,path:str,method="GET",expected={200})->tuple[int,bytes]:
    url=origin+path; opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request=urllib.request.Request(url,method=method,headers={"Accept":"application/json" if path=="/healthz" else "text/html"})
    try:
        response=opener.open(request,timeout=12)
    except urllib.error.HTTPError as error_response:
        response=error_response
    except (OSError,urllib.error.URLError,ssl.SSLError) as exc:raise PostInstallError(f"post-install HTTP probe failed: {path}") from exc
    with response:
        if response.geturl()!=url or int(response.status) not in expected:raise PostInstallError(f"post-install HTTP status differs: {path}")
        raw=response.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise PostInstallError(f"post-install HTTP response is too large: {path}")
    return int(response.status),raw


def retry_request(origin:str,path:str,method="GET",expected={200})->tuple[int,bytes]:
    last=None
    for attempt in range(3):
        try:return request(origin,path,method,expected)
        except PostInstallError as exc:
            last=exc
            if attempt<2:time.sleep(1)
    raise PostInstallError(f"post-install HTTP probe did not stabilize: {path}") from last


def validate_health(value:dict)->None:
    if not isinstance(value,dict) or value.get("ok") is not True or value.get("active_seats")!=0:
        raise PostInstallError("controller health root differs")
    judge=value.get("realtime_judge")
    if not isinstance(judge,dict) or judge.get("thread_alive") is not True or judge.get("running") is not True \
            or judge.get("error_count")!=0 or judge.get("last_error") not in {"",None}:
        raise PostInstallError("realtime judge health differs")
    counts=judge.get("queue_counts")
    if not isinstance(counts,dict) or any(counts.get(name,0)!=0 for name in UNSAFE_QUEUE):
        raise PostInstallError("realtime judge queue is not quiet")
    notify=value.get("seat_notifications");notify_counts=(notify or {}).get("counts")
    if not isinstance(notify,dict) or notify.get("enabled") is not True or notify.get("healthy") is not True \
            or not isinstance(notify_counts,dict) or any(notify_counts.get(name,0)!=0 for name in
                {"pending","retry","permanent_failed","untracked","missing_resource","invalid_pool"}):
        raise PostInstallError("seat notification health differs")
    desktop=value.get("desktop_access")
    if not isinstance(desktop,dict) or desktop.get("enabled") is not True or desktop.get("healthy") is not True \
            or desktop.get("desired_open") is not False or desktop.get("open") is not False \
            or desktop.get("closed") is not True or desktop.get("managed_count")!=0 \
            or desktop.get("conflict_count")!=0 or desktop.get("management_healthy") is not True \
            or desktop.get("management_missing_count")!=0 or desktop.get("instance_state")!="STOPPED":
        raise PostInstallError("desktop access is not exactly closed")


def database_quiet(path:Path)->None:
    requested=Path(os.path.abspath(path));metadata=os.lstat(requested)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1 \
            or (platform.system().lower()=="linux" and metadata.st_uid!=0):
        raise PostInstallError("orchestrator database metadata is unsafe")
    connection=sqlite3.connect(f"file:{requested}?mode=ro",uri=True,timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        integrity=connection.execute("PRAGMA integrity_check").fetchall()
        if integrity!=[("ok",)]:raise PostInstallError("orchestrator database integrity differs")
        checks=(
            ("SELECT COUNT(*) FROM contests WHERE state IN (?,?,?,?,?)",tuple(sorted(ACTIVE_CONTEST)),"active contest exists"),
            ("SELECT COUNT(*) FROM web_submissions WHERE judge_state IN (?,?,?,?,?)",tuple(sorted(UNSAFE_QUEUE)),"realtime queue is not quiet"),
            ("SELECT COUNT(*) FROM artifact_jobs WHERE state IN ('queued','running')",(),"artifact job is active"),
            ("SELECT COUNT(*) FROM seat_notifications WHERE state<>'sent'",(),"seat notification is pending"),
        )
        for query,parameters,message in checks:
            if connection.execute(query,parameters).fetchone()!=(0,):raise PostInstallError(message)
    finally:connection.close()


def source_pointer(path:Path,release:str)->None:
    requested=Path(os.path.abspath(path));parent=requested.parent.resolve(strict=True)
    current=Path("/") if platform.system().lower()=="linux" else Path(parent.anchor)
    for part in parent.parts[1:]:
        current=current/part;metadata=os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) \
                or (platform.system().lower()=="linux" and (metadata.st_uid!=0 or stat.S_IMODE(metadata.st_mode)&0o022)):
            raise PostInstallError("source pointer ancestor is unsafe")
    metadata=os.lstat(requested)
    if not stat.S_ISLNK(metadata.st_mode) or (platform.system().lower()=="linux" and metadata.st_uid!=0) \
            or os.readlink(requested)!=f"source-releases/{release}":
        raise PostInstallError("current source release differs")


def verify(args)->dict:
    backup=safe_directory(args.backup_directory);tx=safe_directory(args.transaction_directory)
    manifest_raw,_=safe_file(backup/"backup-manifest.json",maximum=4*1024*1024)
    if hashlib.sha256(manifest_raw).hexdigest()!=args.backup_manifest_sha256:raise PostInstallError("backup manifest trust pin differs")
    manifest=json.loads(manifest_raw.decode("utf-8"));validate_manifest(manifest,backup,expected_plan_id=args.plan_id)
    expected=contract(args.expected_contract,args.plan_id,args.source_release)
    source_pointer(Path(expected["source_pointer"]),args.source_release)

    docker=Docker(safe_docker_socket(Path(expected["docker_socket"])))
    live=docker.inspect("noi-orchestrator")
    if not owned(live,args.plan_id,args.source_release) or live.get("Image")!=expected["controller_image_id"] \
            or not (live.get("State") or {}).get("Running"):
        raise PostInstallError("installed controller identity differs")
    health_status,health_raw=request("http://127.0.0.1:8600","/healthz")
    try:health=json.loads(health_raw.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:raise PostInstallError("controller health is invalid JSON") from exc
    if health_status!=200:raise PostInstallError("controller health status differs")
    validate_health(health);database_quiet(Path(expected["database"]))

    caddyfile_raw,_=safe_file(Path(expected["caddyfile"]),maximum=32*1024*1024)
    snippet_raw,_=safe_file(Path(expected["snippet"]),maximum=4*1024*1024)
    candidate_raw,_=safe_file(tx/f"closed-frontend.{args.plan_id}.Caddyfile",maximum=32*1024*1024)
    closed_raw,_=safe_file(tx/f"closed-frontend.{args.plan_id}.snippet",maximum=4*1024*1024)
    desired_raw,_=safe_file(tx/f"closed-frontend.{args.plan_id}.active.json",maximum=32*1024*1024)
    desired=json.loads(desired_raw.decode("utf-8"))
    if not isinstance(desired,dict):raise PostInstallError("desired Caddy active config differs")
    admin=Admin();live_caddy,_=get_live(admin)
    if caddyfile_raw!=candidate_raw or snippet_raw!=closed_raw or caddy_canonical(live_caddy)!=caddy_canonical(desired) \
            or caddy_canonical(adapt(admin,caddyfile_raw))!=caddy_canonical(desired):
        raise PostInstallError("closed Caddy disk/live state differs")

    for endpoint in ("/orchestrator/submit","/orchestrator/submit/notify","/orchestrator/submit/problem-fileio","/orchestrator/submit/materials"):
        retry_request("http://127.0.0.1:8888",endpoint,method="POST",expected={403})
    retry_request(expected["exam_origin"],"/s/v1-install-closed",expected={503})
    retry_request(expected["exam_origin"],"/healthz",expected={200})
    retry_request(expected["exam_origin"],"/admin",expected={401})

    baseline=json.loads(safe_file(backup/"ordinary-oj-before.json",maximum=1024*1024)[0].decode("utf-8"))
    validate_ordinary(baseline);ordinary=collect_ordinary(expected["oj_origin"],Path(expected["pm2_bin"]));compare_ordinary(baseline,ordinary)
    return {"status":"verified","plan_id":args.plan_id,"controller_id":live["Id"],"controller_image_id":live["Image"],
            "closed":True,"cloud_closed":True,"queues_quiet":True,"ordinary_oj_unchanged":True,"other_mutations":0}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--verify",action="store_true",required=True)
    parser.add_argument("--backup-directory",type=Path,required=True);parser.add_argument("--transaction-directory",type=Path,required=True)
    parser.add_argument("--plan-id",required=True);parser.add_argument("--backup-manifest-sha256",required=True)
    parser.add_argument("--source-release",required=True);parser.add_argument("--expected-contract",type=Path,required=True);args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0 or not HEX64.fullmatch(args.plan_id) \
                or not HEX64.fullmatch(args.backup_manifest_sha256) or not RELEASE.fullmatch(args.source_release):
            raise PostInstallError("post-install verification requires pinned Linux root")
        print(json.dumps(verify(args),sort_keys=True));return 0
    except (PostInstallError,ObservationError,OSError,ValueError,sqlite3.Error,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
