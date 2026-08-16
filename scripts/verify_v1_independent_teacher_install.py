#!/usr/bin/env python3
"""Verify one externally signed independent-teacher install/rollback fact."""
from __future__ import annotations
import argparse, base64, hashlib, json, os, re, stat, subprocess, sys, tempfile
from datetime import datetime
from pathlib import Path

NAMESPACE="noi-v1-independent-teacher-install"
HEX40=re.compile(r"[a-f0-9]{40}"); HEX64=re.compile(r"[a-f0-9]{64}"); IMAGE=re.compile(r"sha256:[a-f0-9]{64}")
MARKER=re.compile(r"NOI-V1-TEACHER-[A-Z0-9]{16,64}"); SIGNER=re.compile(r"[A-Za-z0-9_.@+-]{1,80}")
PUBLIC_KEY=re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?")
class EvidenceError(ValueError): pass
def exact(value,keys,label):
    if not isinstance(value,dict) or set(value)!=set(keys): raise EvidenceError(f"{label} field set differs")
    return value
def canonical(value): return (json.dumps(value,sort_keys=True,separators=(",",":"))+"\n").encode()
def timestamp(value):
    if not isinstance(value,str) or not value.endswith("Z"): raise EvidenceError("teacher install timestamp is invalid")
    try: return datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError as exc: raise EvidenceError("teacher install timestamp is invalid") from exc
def identity(row):
    source=exact(row["source"],{"revision","tree"},"source")
    components=exact(row["components"],{"orchestrator_image_digest","desktop_image_id","desktop_source_revision","hydro_plugin_sha256"},"components")
    if not HEX40.fullmatch(str(source["revision"])) or not HEX40.fullmatch(str(source["tree"])) or not IMAGE.fullmatch(str(components["orchestrator_image_digest"])) \
            or not IMAGE.fullmatch(str(components["desktop_image_id"])) or components["desktop_source_revision"]!=source["revision"] or not HEX64.fullmatch(str(components["hydro_plugin_sha256"])):
        raise EvidenceError("teacher install source/components differ")
    return source,components
def verify_signature(row,ssh_keygen):
    requested=Path(os.path.abspath(ssh_keygen)); resolved=requested.resolve(strict=True); info=resolved.stat()
    if requested!=resolved or not stat.S_ISREG(info.st_mode) or (os.name=="posix" and (info.st_uid!=0 or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)&0o022)):
        raise EvidenceError("teacher install ssh-keygen is unsafe")
    try: signature=base64.b64decode(row["signature"],validate=True)
    except (ValueError,TypeError) as exc: raise EvidenceError("teacher install signature encoding is invalid") from exc
    signed=dict(row); signed.pop("signature")
    with tempfile.TemporaryDirectory(prefix="v1-teacher-install-verify-") as raw:
        allowed=Path(raw)/"allowed"; sig=Path(raw)/"signature"; allowed.write_text(f"{row['signer']} {row['signing_public_key']}\n"); sig.write_bytes(signature)
        result=subprocess.run([str(resolved),"-Y","verify","-f",str(allowed),"-I",row["signer"],"-n",NAMESPACE,"-s",str(sig)],input=canonical(signed),capture_output=True,timeout=10,check=False)
    if result.returncode: raise EvidenceError("teacher install signature is invalid")
def validate(value,*,expected_revision=None,expected_tree=None,expected_components=None,expected_archive_sha256=None,ssh_keygen=None):
    row=exact(value,{"$schema","schema_version","source","components","candidate","observed_at","host","teacher","checks","artifacts","signer","signing_public_key","signature"},"teacher install evidence")
    if row["$schema"]!="v1-independent-teacher-install-evidence.schema.json" or row["schema_version"]!=1: raise EvidenceError("teacher install evidence identity differs")
    source,components=identity(row)
    if expected_revision and source["revision"]!=expected_revision or expected_tree and source["tree"]!=expected_tree or expected_components and components!=expected_components:
        raise EvidenceError("teacher install expected identity differs")
    timestamp(row["observed_at"])
    candidate=exact(row["candidate"],{"manifest_sha256","archive_sha256"},"candidate")
    host=exact(row["host"],{"anonymous_id","architecture","kernel","os_release_sha256"},"host")
    teacher=exact(row["teacher"],{"qualification_marker","independent","operator_id_sha256"},"teacher")
    if any(not HEX64.fullmatch(str(x)) for x in (candidate["manifest_sha256"],candidate["archive_sha256"],host["anonymous_id"],host["os_release_sha256"],teacher["operator_id_sha256"])) \
            or not MARKER.fullmatch(str(teacher["qualification_marker"])) or teacher["independent"] is not True \
            or not all(isinstance(host[x],str) and 1<=len(host[x])<=200 for x in ("architecture","kernel")):
        raise EvidenceError("teacher install candidate/host/operator identity differs")
    if expected_archive_sha256 and candidate["archive_sha256"]!=expected_archive_sha256:
        raise EvidenceError("teacher install candidate archive differs")
    checks=exact(row["checks"],{"candidate_verified","clean_target","root_only_staging","closed_frontend","controller_healthy","active_seats","managed_rules","cloud_state","ordinary_oj_errors","ordinary_oj_restarts","ordinary_oj_pid_changes","rollback_verified","pending_markers"},"checks")
    for key in ("candidate_verified","clean_target","root_only_staging","closed_frontend","controller_healthy","rollback_verified"):
        if checks[key] is not True: raise EvidenceError(f"teacher install {key} differs")
    for key in ("active_seats","managed_rules","ordinary_oj_errors","ordinary_oj_restarts","ordinary_oj_pid_changes","pending_markers"):
        if checks[key] != 0: raise EvidenceError(f"teacher install {key} must equal zero")
    if checks["cloud_state"]!="STOPPED": raise EvidenceError("teacher install cloud state differs")
    artifacts=exact(row["artifacts"],{"install_log_sha256","rollback_receipt_sha256","ordinary_oj_before_sha256",
        "ordinary_oj_after_sha256","clean_install_rehearsal_sha256"},"artifacts")
    if any(not HEX64.fullmatch(str(value)) for value in artifacts.values()) or artifacts["ordinary_oj_before_sha256"]!=artifacts["ordinary_oj_after_sha256"]:
        raise EvidenceError("teacher install artifacts or ordinary OJ baseline differ")
    if not SIGNER.fullmatch(str(row["signer"])) or not PUBLIC_KEY.fullmatch(str(row["signing_public_key"])): raise EvidenceError("teacher install signer differs")
    if ssh_keygen is not None: verify_signature(row,ssh_keygen)
    return row
def read(path):
    descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
    try:
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not 0<info.st_size<=4*1024*1024: raise EvidenceError("teacher install evidence file is unsafe")
        raw=os.read(descriptor,info.st_size+1)
        if len(raw)!=info.st_size: raise EvidenceError("teacher install evidence changed while reading")
    finally: os.close(descriptor)
    try: return raw,json.loads(raw.decode())
    except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise EvidenceError("teacher install evidence is invalid JSON") from exc
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("evidence",type=Path); parser.add_argument("--ssh-keygen",default="/usr/bin/ssh-keygen",type=Path); args=parser.parse_args()
    try:
        raw,value=read(args.evidence); row=validate(value,ssh_keygen=args.ssh_keygen); print(json.dumps({"evidence_sha256":hashlib.sha256(raw).hexdigest(),"revision":row["source"]["revision"],"status":"passed"},sort_keys=True)); return 0
    except (EvidenceError,OSError,subprocess.SubprocessError) as exc: print(f"NO_GO: {exc}",file=sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
