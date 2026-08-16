#!/usr/bin/env python3
"""Qualification-only deterministic SIGKILL and image-transaction recovery."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import stat
import subprocess
import time

SIGKILL = 9

EMBEDDED_CONFIG = None
HEX40 = re.compile(r"[a-f0-9]{40}")
HEX64 = re.compile(r"[a-f0-9]{64}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
MARKER = re.compile(r"NOI-V1-QUAL-[A-Z0-9]{16,64}")
TAG = re.compile(r"(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?")

class AgentError(RuntimeError): pass

def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys): raise AgentError(f"{label} field set differs")
    return value

def canonical(value): return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
def now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

def absolute(value, label):
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or "\0" in value or ".." in PurePosixPath(value).parts or "//" in value:
        raise AgentError(f"{label} must be a normalized absolute path")
    return value

def validate_config(value):
    keys = {"schema_version","qualification_marker","session_id","source","components","common_library_path","common_library_sha256","docker_socket",
        "app_root","promotion_script","promotion_script_sha256","recovery_script","recovery_script_sha256","bash_path","candidate_tag",
        "candidate_image_id","source_root","source_revision","old_image_id","old_source_target","ready_path","controller_health","ordinary_oj",
        "signer","signing_public_key","signing_key_path","ssh_keygen_path","lock_path","state_path","receipt_path","output_path"}
    row = exact(value, keys, "power loss configuration")
    if row["schema_version"] != 1 or not MARKER.fullmatch(str(row["qualification_marker"])) or not HEX64.fullmatch(str(row["session_id"])) \
            or not HEX40.fullmatch(str(row["source_revision"])) or row["docker_socket"] != "/var/run/docker.sock" or row["bash_path"] != "/bin/bash" \
            or not TAG.fullmatch(str(row["candidate_tag"])) or row["candidate_tag"].endswith(":latest") \
            or not IMAGE.fullmatch(str(row["candidate_image_id"])) or not IMAGE.fullmatch(str(row["old_image_id"])) \
            or not re.fullmatch(r"image-releases/[A-Za-z0-9TZ-]+", str(row["old_source_target"])):
        raise AgentError("power loss identity is invalid")
    source = exact(row["source"], {"revision","tree"}, "source")
    components = exact(row["components"], {"orchestrator_image_digest","desktop_image_id","desktop_source_revision","hydro_plugin_sha256"}, "components")
    if source["revision"] != row["source_revision"] or not HEX40.fullmatch(str(source["tree"])) or components["desktop_source_revision"] != row["source_revision"] \
            or not IMAGE.fullmatch(str(components["orchestrator_image_digest"])) or components["desktop_image_id"] != row["candidate_image_id"] \
            or not HEX64.fullmatch(str(components["hydro_plugin_sha256"])):
        raise AgentError("power loss component identity differs")
    for key in ("common_library_sha256","promotion_script_sha256","recovery_script_sha256"):
        if not HEX64.fullmatch(str(row[key])): raise AgentError("power loss script identity is invalid")
    for key in ("common_library_path","app_root","promotion_script","recovery_script","bash_path","source_root","ready_path","signing_key_path","ssh_keygen_path","lock_path","state_path","receipt_path","output_path"):
        row[key] = absolute(row[key], key)
    if not re.fullmatch(r"/root/[A-Za-z0-9._/-]+[.]json", row["ready_path"]):
        raise AgentError("power loss ready path must be a root-only JSON path")
    health = exact(row["controller_health"], {"url","timeout_seconds"}, "controller health")
    if not re.fullmatch(r"http://127[.]0[.]0[.]1:[0-9]+/healthz", str(health["url"])) or isinstance(health["timeout_seconds"], bool) or not isinstance(health["timeout_seconds"], int) or not 1 <= health["timeout_seconds"] <= 10:
        raise AgentError("power loss health configuration differs")
    ordinary = exact(row["ordinary_oj"], {"pm2_path","pm2_home","processes","http_probes"}, "ordinary OJ")
    if not isinstance(ordinary["processes"], list) or len(ordinary["processes"]) != 4 or not isinstance(ordinary["http_probes"], list) or not 3 <= len(ordinary["http_probes"]) <= 6:
        raise AgentError("power loss ordinary OJ baseline differs")
    names = set()
    for item in ordinary["processes"]:
        item = exact(item, {"name","pid","restart_time","status"}, "ordinary OJ process")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(item["name"])) or item["name"] in names or item["status"] != "online" \
                or isinstance(item["pid"], bool) or not isinstance(item["pid"], int) or item["pid"] <= 0 \
                or isinstance(item["restart_time"], bool) or not isinstance(item["restart_time"], int) or item["restart_time"] < 0:
            raise AgentError("power loss ordinary OJ process differs")
        names.add(item["name"])
    for item in ordinary["http_probes"]:
        item = exact(item, {"url","host","status","body_contains"}, "ordinary OJ probe")
        if not re.fullmatch(r"http://127[.]0[.]0[.]1:[0-9]+/[^?#]*", str(item["url"])) or item["status"] != 200 \
                or not isinstance(item["host"], str) or not isinstance(item["body_contains"], str):
            raise AgentError("power loss ordinary OJ probe differs")
    ordinary["pm2_path"] = absolute(ordinary["pm2_path"], "pm2_path"); ordinary["pm2_home"] = absolute(ordinary["pm2_home"], "pm2_home")
    if len({row[k] for k in ("ready_path","lock_path","state_path","receipt_path","output_path")}) != 5 \
            or not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,80}", str(row["signer"])) \
            or not PUBLIC_KEY.fullmatch(str(row["signing_public_key"])):
        raise AgentError("power loss private paths or signer differ")
    return row

def load_common(row):
    path = Path(row["common_library_path"]); raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row["common_library_sha256"]: raise AgentError("power loss common library SHA256 differs")
    spec = importlib.util.spec_from_file_location("v1_power_loss_common", path)
    if spec is None or spec.loader is None: raise AgentError("power loss common library cannot load")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

def script(row, common, name):
    path = common.regular(Path(row[name]), name, executable=True)
    if common.file_sha256(path) != row[name + "_sha256"]: raise AgentError(f"{name} SHA256 differs")
    return path

def baseline(row, common):
    app = Path(row["app_root"]); current = app / "current-image-source"
    requested = Path(os.path.abspath(app)); resolved = requested.resolve(strict=True); common.safe_ancestors(app / ".qualification-boundary")
    info = resolved.lstat()
    if requested != resolved or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise AgentError("power loss application root metadata is unsafe")
    if not current.is_symlink() or os.readlink(current) != row["old_source_target"]: raise AgentError("power loss source baseline differs")
    raw = common.docker_request(row, "GET", "/images/noi-linux-official:2.0/json", expected={200})
    if json.loads(raw).get("Id") != row["old_image_id"]: raise AgentError("power loss image baseline differs")
    filters = json.dumps({"label":["noi.contest"]}, separators=(",", ":"))
    raw = common.docker_request(row, "GET", "/containers/json?filters=" + __import__("urllib.parse").parse.quote(filters), expected={200})
    if json.loads(raw) != []: raise AgentError("power loss qualification requires zero active seats")
    status, health_raw = common.local_http(row["controller_health"]["url"], timeout=row["controller_health"]["timeout_seconds"])
    health = json.loads(health_raw)
    desktop = health.get("desktop_access") or {}
    if status != 200 or health.get("ok") is not True or desktop.get("managed_count") != 0 or desktop.get("instance_state") != "STOPPED":
        raise AgentError("power loss qualification cloud baseline is not closed")
    return common.ordinary_snapshot(row)

def command(row, promotion):
    if promotion:
        return [row["bash_path"], row["promotion_script"], "--image", row["candidate_tag"], "--expected-image-id", row["candidate_image_id"],
                "--source-root", row["source_root"], "--source-revision", row["source_revision"]]
    return [row["bash_path"], row["recovery_script"]]

def wait_ready(row, process):
    path = Path(row["ready_path"]); end = time.monotonic() + 90
    while time.monotonic() < end:
        if process.poll() is not None: raise AgentError("promotion exited before the durable crash boundary")
        if path.is_file() and not path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or \
                    (os.name == "posix" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077)):
                raise AgentError("power loss ready fact metadata is unsafe")
            value = json.loads(path.read_text())
            if value == {"schema_version":1,"qualification_marker":row["qualification_marker"],"phase":"marker_durable_before_mutation","pid":process.pid}:
                if platform.system().lower() != "linux": return
                try:
                    fields = (Path("/proc") / str(process.pid) / "status").read_text().splitlines()
                except (OSError, UnicodeError):
                    time.sleep(.2); continue
                states = [line.split(":", 1)[1].strip() for line in fields if line.startswith("State:")]
                if len(states) == 1 and states[0].startswith("T"):
                    return
        time.sleep(.2)
    raise AgentError("promotion did not reach the durable crash boundary")

def run_program(argv, env, *, expect=0):
    result = subprocess.run(argv, capture_output=True, check=False, timeout=180, env=env)
    if result.returncode != expect: raise AgentError("power loss child command result differs")
    return result

def run(config, *, runtime_path=None):
    row = validate_config(config); common = load_common(row); agent_sha = common.file_sha256(Path(__file__) if runtime_path is None else runtime_path)
    promotion = script(row, common, "promotion_script"); recovery = script(row, common, "recovery_script")
    lock = common.acquire_lock(Path(row["lock_path"])); state = Path(row["state_path"]); ready = Path(row["ready_path"])
    process = None
    try:
        if any(os.path.lexists(p) for p in (state, ready, row["receipt_path"], row["output_path"])): raise AgentError("power loss private path already exists")
        if any(os.path.lexists(Path(row["app_root"]) / name) for name in ("image-promotion.pending", "image-promotion-recovery.receipt")):
            raise AgentError("power loss application root contains an earlier transaction")
        signature_check = {"schema_version":1,"qualification_marker":row["qualification_marker"],"session_id":row["session_id"],"purpose":"preflight-signature-check"}
        common.verify_signature(row, signature_check, common.sign(row, signature_check))
        ordinary = baseline(row, common); started = now()
        common.atomic_write(state, canonical({"schema_version":1,"qualification_marker":row["qualification_marker"],"phase":"armed"}))
        env = {"HOME":"/root","USER":"root","LOGNAME":"root","PATH":"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               "LANG":"C","LC_ALL":"C","NOI_APP_ROOT":row["app_root"],"NOI_V1_QUALIFICATION_MARKER":row["qualification_marker"],
               "NOI_V1_POWER_LOSS_READY_PATH":row["ready_path"]}
        process = subprocess.Popen(command(row, True), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        wait_ready(row, process)
        marker = Path(row["app_root"]) / "image-promotion.pending"
        marker_info = marker.lstat()
        if not stat.S_ISREG(marker_info.st_mode) or (os.name == "posix" and marker_info.st_uid != 0) or marker_info.st_nlink != 1 or \
                (os.name == "posix" and stat.S_IMODE(marker_info.st_mode) != 0o600):
            raise AgentError("power loss pending marker metadata is unsafe")
        marker_raw = marker.read_bytes(); marker_sha = hashlib.sha256(marker_raw).hexdigest()
        baseline(row, common)
        os.kill(process.pid, SIGKILL); return_code = process.wait(timeout=15)
        if return_code != -SIGKILL: raise AgentError("power loss abrupt termination was not observed")
        blocked = run_program(command(row, True), {k:v for k,v in env.items() if not k.startswith("NOI_V1_")}, expect=1)
        if b"unfinished image transaction" not in blocked.stderr: raise AgentError("power loss pending marker did not block startup")
        recovery_command = command(row, False) + ["--expected-marker-sha256", marker_sha]
        clean_env = {k:v for k,v in env.items() if not k.startswith("NOI_V1_")}
        run_program(recovery_command, clean_env); run_program(recovery_command, clean_env)
        if marker.exists() or not (Path(row["app_root"]) / "image-promotion-recovery.receipt").is_file(): raise AgentError("power loss recovery artifacts differ")
        if baseline(row, common) != ordinary: raise AgentError("power loss ordinary OJ baseline changed")
        ended = now(); receipt = {"schema_version":1,"qualification_marker":row["qualification_marker"],"session_id":row["session_id"],
            "marker_sha256":marker_sha,"started_at":started,"ended_at":ended}
        receipt_raw = canonical(receipt); common.atomic_write(Path(row["receipt_path"]), receipt_raw)
        action = {"$schema":"v1-fault-recovery-action-fact.schema.json","schema_version":1,"kind":"fault_recovery_action","scenario":"power_loss_recovery",
            "session_id":row["session_id"],"source":row["source"],"components":row["components"],"qualification_marker":row["qualification_marker"],
            "started_at":started,"ended_at":ended,"collector":{"mode":"trusted_action_agent","agent_sha256":agent_sha},"signer":row["signer"],
            "signing_public_key":row["signing_public_key"],"payload":{"ordinary_oj_errors":0,"ordinary_oj_restarts":0,"ordinary_oj_pid_changes":0,
            "duplicate_oj_records":0,"final_source_mismatches":0,"other_seat_failures":0,"durable_marker_created":True,
            "abrupt_termination_observed":True,"startup_blocked_pending":True,"recovery_completed":True,"baseline_restored":True,
            "active_seats":0,"managed_rules":0,"cloud_state":"STOPPED"}}
        signature = common.sign(row, action); common.verify_signature(row, action, signature); action["signature"] = signature
        common.atomic_write(Path(row["output_path"]), canonical(action)); common.unlink_durable(state); common.unlink_durable(ready)
        return {"status":"passed","marker_sha256":marker_sha,"receipt_sha256":hashlib.sha256(receipt_raw).hexdigest()}
    finally:
        if process is not None and process.poll() is None:
            try: os.kill(process.pid, SIGKILL); process.wait(timeout=15)
            except (OSError, subprocess.SubprocessError): pass
        os.close(lock)

def main():
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0: raise AgentError("power loss recovery agent requires Linux root")
        if EMBEDDED_CONFIG is None: raise AgentError("power loss recovery agent is not frozen")
        print(json.dumps(run(EMBEDDED_CONFIG), sort_keys=True, separators=(",", ":"))); return 0
    except (AgentError,OSError,subprocess.SubprocessError,UnicodeError,json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=os.sys.stderr); return 2

if __name__ == "__main__": raise SystemExit(main())
