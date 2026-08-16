#!/usr/bin/env python3
"""Conditionally commit one fully adapted Caddy config with ETag protection."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tempfile


class CaddyCommitError(RuntimeError): pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def safe_file(path: Path, label: str, maximum: int = 32 * 1024 * 1024) -> bytes:
    requested=Path(os.path.abspath(path)); resolved=requested.resolve(strict=True); metadata=os.lstat(requested)
    if requested!=resolved or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1 or not 0<metadata.st_size<=maximum:
        raise CaddyCommitError(f"{label} metadata is unsafe")
    if platform.system().lower()=="linux" and metadata.st_uid!=0:
        raise CaddyCommitError(f"{label} is not root-owned")
    descriptor=os.open(resolved,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
    try:
        opened=os.fstat(descriptor); raw=os.read(descriptor,opened.st_size+1)
        if len(raw)!=opened.st_size or (opened.st_dev,opened.st_ino)!=(metadata.st_dev,metadata.st_ino):
            raise CaddyCommitError(f"{label} changed while reading")
        return raw
    finally: os.close(descriptor)


def json_value(raw: bytes, label: str):
    try: value=json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise CaddyCommitError(f"{label} is invalid JSON") from exc
    if not isinstance(value,dict): raise CaddyCommitError(f"{label} is not an object")
    return value


class Admin:
    def __init__(self, host="127.0.0.1", port=2019, timeout=10): self.host=host; self.port=port; self.timeout=timeout
    def request(self, method, path, body=None, headers=None):
        connection=http.client.HTTPConnection(self.host,self.port,timeout=self.timeout)
        try:
            connection.request(method,path,body=body,headers=headers or {})
            response=connection.getresponse(); data=response.read(32*1024*1024+1)
            if len(data)>32*1024*1024: raise CaddyCommitError("Caddy admin response is too large")
            return response.status,{key.lower():value for key,value in response.getheaders()},data
        finally: connection.close()


def get_live(admin: Admin):
    status,headers,raw=admin.request("GET","/config/")
    if status!=200: raise CaddyCommitError("Caddy active config GET failed")
    etag=headers.get("etag")
    if not isinstance(etag,str) or not etag or "\r" in etag or "\n" in etag:
        raise CaddyCommitError("Caddy active config has no safe ETag")
    return json_value(raw,"Caddy active config"),etag


def adapt(admin: Admin, caddyfile: bytes):
    status,_,raw=admin.request("POST","/adapt",body=caddyfile,headers={"Content-Type":"text/caddyfile"})
    if status!=200: raise CaddyCommitError("Caddy adapt failed")
    outer=json_value(raw,"Caddy adapt response")
    if set(outer)!={"warnings","result"} or not isinstance(outer["warnings"],list) or not isinstance(outer["result"],dict):
        raise CaddyCommitError("Caddy adapt response shape differs")
    return outer["result"]


def commit(admin: Admin, expected: dict, desired: dict, *, reread_disk, expected_disk_sha256: str):
    live,etag=get_live(admin)
    if canonical(live)!=canonical(expected): raise CaddyCommitError("Caddy active config differs from transaction baseline")
    if hashlib.sha256(reread_disk()).hexdigest()!=expected_disk_sha256:
        raise CaddyCommitError("Caddy candidate file changed before conditional commit")
    body=canonical(desired)
    status,_,_=admin.request("POST","/config/",body=body,headers={"Content-Type":"application/json","If-Match":etag})
    if status==412: raise CaddyCommitError("Caddy conditional commit lost an ETag race")
    if status not in {200,201}: raise CaddyCommitError("Caddy conditional commit failed")
    actual,_=get_live(admin)
    if canonical(actual)!=body: raise CaddyCommitError("Caddy active config differs after conditional commit")
    return {"status":"verified","etag_used":True,"active_sha256":hashlib.sha256(body).hexdigest()}


def restore(admin: Admin, desired: dict, baseline: dict):
    live,etag=get_live(admin)
    live_raw=canonical(live); desired_raw=canonical(desired); baseline_raw=canonical(baseline)
    if live_raw==baseline_raw:
        return {"status":"verified","already_restored":True,
                "active_sha256":hashlib.sha256(baseline_raw).hexdigest()}
    if live_raw!=desired_raw:
        raise CaddyCommitError("Caddy active config changed outside this transaction; refusing rollback overwrite")
    status,_,_=admin.request("POST","/config/",body=baseline_raw,
                             headers={"Content-Type":"application/json","If-Match":etag})
    if status==412: raise CaddyCommitError("Caddy conditional rollback lost an ETag race")
    if status not in {200,201}: raise CaddyCommitError("Caddy conditional rollback failed")
    actual,_=get_live(admin)
    if canonical(actual)!=baseline_raw:
        raise CaddyCommitError("Caddy active config differs after conditional rollback")
    return {"status":"verified","already_restored":False,
            "active_sha256":hashlib.sha256(baseline_raw).hexdigest()}


def atomic_json(path: Path, value: dict):
    if os.path.lexists(path): raise CaddyCommitError("desired Caddy JSON output already exists")
    descriptor,temporary=tempfile.mkstemp(prefix=".caddy-desired.",dir=path.parent)
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


def main():
    parser=argparse.ArgumentParser(); operation=parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--commit",action="store_true"); operation.add_argument("--restore",action="store_true")
    parser.add_argument("--candidate-caddyfile",type=Path); parser.add_argument("--expected-live-json",required=True,type=Path)
    parser.add_argument("--desired-live-json",type=Path); parser.add_argument("--output-desired-json",type=Path)
    args=parser.parse_args()
    try:
        if platform.system().lower()!="linux" or os.geteuid()!=0: raise CaddyCommitError("Caddy commit requires Linux root")
        expected=json_value(safe_file(args.expected_live_json,"expected Caddy live JSON"),"expected Caddy live JSON")
        admin=Admin()
        if args.commit:
            if args.candidate_caddyfile is None or args.output_desired_json is None or args.desired_live_json is not None:
                raise CaddyCommitError("commit arguments differ")
            caddyfile=safe_file(args.candidate_caddyfile,"candidate Caddyfile"); disk_sha=hashlib.sha256(caddyfile).hexdigest()
            desired=adapt(admin,caddyfile); atomic_json(args.output_desired_json,desired)
            result=commit(admin,expected,desired,reread_disk=lambda:safe_file(args.candidate_caddyfile,"candidate Caddyfile"),expected_disk_sha256=disk_sha)
        else:
            if args.desired_live_json is None or args.candidate_caddyfile is not None or args.output_desired_json is not None:
                raise CaddyCommitError("restore arguments differ")
            desired=json_value(safe_file(args.desired_live_json,"desired Caddy live JSON"),"desired Caddy live JSON")
            result=restore(admin,desired,expected)
        print(json.dumps(result,sort_keys=True)); return 0
    except (CaddyCommitError,OSError,http.client.HTTPException) as exc:
        print(f"NO_GO: {exc}",file=sys.stderr); return 2


if __name__=="__main__": raise SystemExit(main())
