#!/usr/bin/env python3
"""Qualification-only one-failure, one-retry collection evidence agent."""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import sqlite3
import stat
import subprocess
import time
from urllib.parse import quote, urlencode, urlsplit

import yaml

EMBEDDED_CONFIG = None
NAMESPACE = "noi-v1-fault-recovery-actions"
HEX24 = re.compile(r"[a-f0-9]{24}")
HEX40 = re.compile(r"[a-f0-9]{40}")
HEX64 = re.compile(r"[a-f0-9]{64}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
MARKER = re.compile(r"NOI-V1-QUAL-[A-Z0-9]{16,64}")
PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?")
ROW_COLUMNS = ("id", "tid", "uid", "problem", "sha256", "size", "submission_id", "submission_session",
               "judge_pid", "judge_lang", "judge_sha256", "judge_state", "judge_kind", "accepted_at_ms", "rid", "attempts")


class AgentError(RuntimeError): pass


def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys): raise AgentError(f"{label} field set differs")
    return value


def canonical(value): return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
def utc_now(): return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def absolute(value, label):
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or "\x00" in value or \
            ".." in PurePosixPath(value).parts or "//" in value: raise AgentError(f"{label} must be a normalized absolute path")
    return value


def validate_config(value):
    keys = {"schema_version", "qualification_marker", "session_id", "contest_id", "source", "components",
        "common_library_path", "common_library_sha256", "docker_socket", "controller", "database_path", "expected_failed_row_sha256",
        "failure_marker_host_path", "failure_marker_container_path", "controller_config_host_path",
        "controller_config_sha256", "collected_root_host_path", "collected_root_container_path", "admin_url",
        "admin_credentials_path", "controller_health", "submission_status", "ordinary_oj", "signer",
        "signing_public_key", "signing_key_path", "ssh_keygen_path", "lock_path", "recovery_state_path",
        "receipt_path", "output_path"}
    row = exact(value, keys, "collection retry configuration")
    if row["schema_version"] != 1 or not MARKER.fullmatch(str(row["qualification_marker"])) or \
            not HEX64.fullmatch(str(row["session_id"])) or not HEX24.fullmatch(str(row["contest_id"])) or \
            row["docker_socket"] != "/var/run/docker.sock" or \
            any(not HEX64.fullmatch(str(row[key])) for key in ("common_library_sha256", "expected_failed_row_sha256", "controller_config_sha256")):
        raise AgentError("collection retry identity is invalid")
    source = exact(row["source"], {"revision", "tree"}, "source")
    components = exact(row["components"], {"orchestrator_image_digest", "desktop_image_id",
        "desktop_source_revision", "hydro_plugin_sha256"}, "components")
    controller = exact(row["controller"], {"container_id", "image_id", "name", "identity_sha256", "restart_count"}, "controller")
    if not HEX40.fullmatch(str(source["revision"])) or not HEX40.fullmatch(str(source["tree"])) or \
            source["revision"] != components["desktop_source_revision"] or \
            not IMAGE.fullmatch(str(components["orchestrator_image_digest"])) or not IMAGE.fullmatch(str(components["desktop_image_id"])) or \
            not HEX64.fullmatch(str(components["hydro_plugin_sha256"])) or not HEX64.fullmatch(str(controller["container_id"])) or \
            controller["image_id"] != components["orchestrator_image_digest"] or not HEX64.fullmatch(str(controller["identity_sha256"])) or \
            not re.fullmatch(r"/[A-Za-z0-9_.-]{1,127}", str(controller["name"])) or isinstance(controller["restart_count"], bool) or \
            not isinstance(controller["restart_count"], int) or controller["restart_count"] < 0:
        raise AgentError("collection retry component identity is invalid")
    health = exact(row["controller_health"], {"url", "timeout_seconds", "deadline_seconds"}, "controller health")
    status = exact(row["submission_status"], {"url", "token_path", "timeout_seconds"}, "submission status")
    if not re.fullmatch(r"http://127[.]0[.]0[.]1:[0-9]+/admin", str(row["admin_url"])) or \
            not re.fullmatch(r"http://127[.]0[.]0[.]1:[0-9]+/healthz", str(health["url"])) or \
            not re.fullmatch(r"http://127[.]0[.]0[.]1:[0-9]+/orchestrator/submit/status", str(status["url"])):
        raise AgentError("collection retry local endpoint is invalid")
    for number, low, high in ((health["timeout_seconds"], 1, 10), (health["deadline_seconds"], 30, 600), (status["timeout_seconds"], 1, 10)):
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high: raise AgentError("collection retry timeout is invalid")
    ordinary = exact(row["ordinary_oj"], {"pm2_path", "pm2_home", "processes", "http_probes"}, "ordinary OJ")
    if not isinstance(ordinary["processes"], list) or len(ordinary["processes"]) != 4 or \
            not isinstance(ordinary["http_probes"], list) or not 3 <= len(ordinary["http_probes"]) <= 6:
        raise AgentError("collection retry ordinary OJ baseline is invalid")
    names = set()
    for item in ordinary["processes"]:
        item = exact(item, {"name", "pid", "restart_time", "status"}, "ordinary OJ process")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(item["name"])) or item["name"] in names or \
                isinstance(item["pid"], bool) or not isinstance(item["pid"], int) or item["pid"] <= 0 or \
                isinstance(item["restart_time"], bool) or not isinstance(item["restart_time"], int) or item["restart_time"] < 0 or \
                item["status"] != "online": raise AgentError("collection retry ordinary OJ process is invalid")
        names.add(item["name"])
    for item in ordinary["http_probes"]:
        item = exact(item, {"url", "host", "status", "body_contains"}, "ordinary OJ probe")
        parsed = urlsplit(str(item["url"]))
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.query or parsed.fragment or \
                item["status"] != 200 or not isinstance(item["body_contains"], str):
            raise AgentError("collection retry ordinary OJ probe is invalid")
    for key in ("common_library_path", "database_path", "failure_marker_host_path", "failure_marker_container_path",
                "controller_config_host_path", "collected_root_host_path", "collected_root_container_path",
                "admin_credentials_path", "signing_key_path", "ssh_keygen_path", "lock_path", "recovery_state_path",
                "receipt_path", "output_path"):
        row[key] = absolute(row[key], key)
    if not re.fullmatch(
        r"/app/data/qualification/[A-Za-z0-9_.-]{1,128}[.]json",
        row["failure_marker_container_path"],
    ):
        raise AgentError("collection retry container marker path is outside the qualification directory")
    status["token_path"] = absolute(status["token_path"], "token_path")
    ordinary["pm2_path"] = absolute(ordinary["pm2_path"], "pm2_path"); ordinary["pm2_home"] = absolute(ordinary["pm2_home"], "pm2_home")
    if len({row[k] for k in ("failure_marker_host_path", "admin_credentials_path", "signing_key_path", "lock_path",
                              "recovery_state_path", "receipt_path", "output_path")}) != 7 or \
            not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,80}", str(row["signer"])) or not PUBLIC_KEY.fullmatch(str(row["signing_public_key"])):
        raise AgentError("collection retry private paths or signer are invalid")
    return row


def load_common(config):
    path = Path(config["common_library_path"]); raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != config["common_library_sha256"]: raise AgentError("collection retry common library SHA256 differs")
    spec = importlib.util.spec_from_file_location("v1_collection_retry_common", path)
    if spec is None or spec.loader is None: raise AgentError("collection retry common library cannot load")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def db_connect(config):
    path = Path(config["database_path"]); uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=3); connection.row_factory = sqlite3.Row; connection.execute("PRAGMA query_only=ON")
    return connection


def contest(config):
    with closing(db_connect(config)) as connection:
        row = connection.execute("SELECT tid,state,files,pids,collection_run_id,collection_dir,collection_receipt_sha256 FROM contests WHERE tid=?", (config["contest_id"],)).fetchone()
    if not row: raise AgentError("collection retry contest is missing")
    return dict(row)


def failed_submission(config):
    with closing(db_connect(config)) as connection:
        rows = connection.execute("SELECT " + ",".join(ROW_COLUMNS) + " FROM web_submissions WHERE judge_state='permanent_failed' ORDER BY id").fetchall()
        seats = connection.execute("SELECT uid FROM seats WHERE tid=?", (config["contest_id"],)).fetchall()
    values = [dict(row) for row in rows]
    if len(values) != 1 or values[0]["tid"] != config["contest_id"] or not HEX64.fullmatch(str(values[0]["submission_id"])) or \
            hashlib.sha256(canonical(values[0])).hexdigest() != config["expected_failed_row_sha256"]:
        raise AgentError("collection retry requires one exact failed qualification row")
    document = contest(config)
    try: files, pids = json.loads(document["files"]), json.loads(document["pids"])
    except (TypeError, json.JSONDecodeError) as exc: raise AgentError("collection retry contest mapping is invalid") from exc
    if not isinstance(files, dict) or len(files) != 1 or set(files) != {values[0]["problem"]} or \
            not isinstance(pids, dict) or set(pids) != set(files) or str(pids[values[0]["problem"]]) != str(values[0]["judge_pid"]) or \
            len(seats) != 1 or int(seats[0]["uid"]) != int(values[0]["uid"]):
        raise AgentError("collection retry must use one seat and one problem")
    with closing(db_connect(config)) as connection:
        latest = connection.execute("SELECT MAX(id) FROM web_submissions WHERE tid=? AND uid=? AND problem=?",
            (config["contest_id"], int(values[0]["uid"]), values[0]["problem"])).fetchone()[0]
    if int(latest or 0) != int(values[0]["id"]): raise AgentError("collection retry failed row is not the latest source")
    return values[0]


def submission_by_id(config, row_id):
    with closing(db_connect(config)) as connection:
        row = connection.execute("SELECT " + ",".join(ROW_COLUMNS) + " FROM web_submissions WHERE id=?", (int(row_id),)).fetchone()
    return dict(row) if row else None


def wait_final_delivered(config, frozen, deadline):
    end = time.monotonic() + deadline
    dynamic = {"judge_state", "judge_kind", "rid", "attempts"}
    immutable = {key: value for key, value in frozen.items() if key not in dynamic}
    while time.monotonic() < end:
        current = submission_by_id(config, frozen["id"])
        if current:
            if {key: value for key, value in current.items() if key not in dynamic} != immutable:
                raise AgentError("collection retry source identity changed")
            if current["judge_state"] == "submitted" and current["judge_kind"] == "final" and \
                    HEX24.fullmatch(str(current["rid"])) and int(current["attempts"]) > int(frozen["attempts"]):
                return current
        time.sleep(1)
    raise AgentError("collection retry final delivery did not complete")


def validate_mounts_and_config(config, common):
    value, identity = common.inspect_controller(config, running=True)
    mounts = value.get("Mounts") or []
    def mapping(host, container, *, allow_parent=False):
        host_path, container_path = Path(host), PurePosixPath(container)
        for item in mounts:
            source, target = Path(str(item.get("Source") or "")), PurePosixPath(str(item.get("Destination") or ""))
            if source == host_path and target == container_path: return True
            if allow_parent:
                try:
                    relative = host_path.relative_to(source)
                    if target / PurePosixPath(relative.as_posix()) == container_path: return True
                except (ValueError, TypeError): pass
        return False
    if not mapping(config["controller_config_host_path"], "/app/config.yaml") or \
            not mapping(config["failure_marker_host_path"], config["failure_marker_container_path"], allow_parent=True) or \
            not mapping(config["collected_root_host_path"], config["collected_root_container_path"], allow_parent=True):
        raise AgentError("collection retry host/container path mapping differs")
    path = common.regular(Path(config["controller_config_host_path"]), "controller config", private=True)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != config["controller_config_sha256"]: raise AgentError("collection retry controller config SHA256 differs")
    try: parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc: raise AgentError("collection retry controller config is invalid") from exc
    hydro = parsed.get("hydro") if isinstance(parsed, dict) else None
    if not isinstance(hydro, dict) or hydro.get("qualification_failure_marker_path") != config["failure_marker_container_path"] or \
            hydro.get("qualification_marker") != config["qualification_marker"]:
        raise AgentError("collection retry qualification marker is not enabled in frozen config")
    return identity


def credentials(config, common):
    path = common.regular(Path(config["admin_credentials_path"]), "admin credentials", private=True)
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise AgentError("collection retry admin credentials are invalid") from exc
    value = exact(value, {"username", "password"}, "admin credentials")
    if not all(isinstance(value[k], str) and 1 <= len(value[k]) <= 4096 for k in value): raise AgentError("collection retry admin credentials are invalid")
    return value


def admin_request(config, common, method, path, auth, body=None):
    encoded = base64.b64encode(f"{auth['username']}:{auth['password']}".encode()).decode()
    headers = {"Authorization": "Basic " + encoded, "Connection": "close"}
    if body is not None: headers["Content-Type"] = "application/x-www-form-urlencoded"
    parsed = urlsplit(config["admin_url"]); url = f"http://127.0.0.1:{parsed.port}{path}"
    return common.local_http(url, method=method, body=body, headers=headers, timeout=10)


def collect_once(config, common, auth):
    status, raw = admin_request(config, common, "GET", "/admin", auth)
    if status != 200: raise AgentError("collection retry admin preflight failed")
    tokens = set(re.findall(rb'name="csrf" value="([A-Za-z0-9_-]{20,200})"', raw))
    if len(tokens) != 1: raise AgentError("collection retry CSRF token is ambiguous")
    body = urlencode({"tid": config["contest_id"], "csrf": next(iter(tokens)).decode()}).encode()
    status, _ = admin_request(config, common, "POST", "/admin/collect", auth, body)
    if status != 303: raise AgentError("collection retry teacher action was not accepted")


def wait_contest(config, state, deadline, *, no_receipt=False):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        row = contest(config)
        if row["state"] == state:
            if no_receipt and any(row[k] for k in ("collection_run_id", "collection_dir", "collection_receipt_sha256")):
                raise AgentError("failed collection unexpectedly produced a receipt")
            return row
        time.sleep(1)
    raise AgentError(f"collection retry contest did not reach {state}")


def marker_bytes(config, frozen):
    return canonical({"schema_version": 1, "qualification_marker": config["qualification_marker"],
        "scenario": "collection_retry", "submission_id": frozen["submission_id"], "failure": "block_until_removed"})


def remove_marker(path):
    try: path.unlink()
    except FileNotFoundError: return
    if os.name == "posix":
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(directory)
        finally: os.close(directory)


def verify_receipt(config, row):
    container_root = PurePosixPath(config["collected_root_container_path"])
    directory = PurePosixPath(str(row["collection_dir"]))
    try: relative = directory.relative_to(container_root)
    except ValueError as exc: raise AgentError("collection retry receipt escaped configured root") from exc
    host_directory = Path(config["collected_root_host_path"]) / Path(relative.as_posix())
    receipt = host_directory / "collection_receipt.json"
    if not receipt.is_file() or receipt.is_symlink(): raise AgentError("collection retry receipt is missing")
    raw = receipt.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
    try: value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise AgentError("collection retry receipt is invalid") from exc
    all_receipts = list((Path(config["collected_root_host_path"]) / config["contest_id"]).glob("*/collection_receipt.json"))
    if len(all_receipts) != 1 or all_receipts[0].resolve() != receipt.resolve() or digest != row["collection_receipt_sha256"] or \
            value.get("submit_failures") != 0 or value.get("run_id") != row["collection_run_id"]:
        raise AgentError("collection retry receipt is not unique or identity-bound")
    return digest


def run(config, *, runtime_path=None):
    row = validate_config(config); common = load_common(row); agent_sha = common.file_sha256(Path(__file__) if runtime_path is None else runtime_path)
    common.regular(Path(row["database_path"]), "orchestrator database", private=True)
    lock = common.acquire_lock(Path(row["lock_path"])); marker = Path(row["failure_marker_host_path"]); state = Path(row["recovery_state_path"])
    handlers = {}
    def interrupted(signum, _frame): raise AgentError(f"collection retry interrupted by signal {signum}")
    try:
        if os.path.lexists(state):
            remove_marker(marker); common.unlink_durable(state)
            raise AgentError("stale collection retry state was recovered; start a new run")
        if os.path.lexists(marker) or os.path.lexists(row["receipt_path"]) or os.path.lexists(row["output_path"]):
            raise AgentError("collection retry private path already exists")
        signature_check = {"schema_version": 1, "qualification_marker": row["qualification_marker"], "session_id": row["session_id"], "purpose": "preflight-signature-check"}
        common.verify_signature(row, signature_check, common.sign(row, signature_check))
        for signum in (signal.SIGINT, signal.SIGTERM): handlers[signum] = signal.getsignal(signum); signal.signal(signum, interrupted)
        ordinary_before = common.ordinary_snapshot(row); common.health_ready(row)
        identity = validate_mounts_and_config(row, common); frozen = failed_submission(row)
        if contest(row)["state"] != "ready": raise AgentError("collection retry contest must start ready")
        auth = credentials(row, common); started = utc_now()
        common.atomic_write(state, canonical({"schema_version": 1, "qualification_marker": row["qualification_marker"], "phase": "prepared"}))
        common.atomic_write(marker, marker_bytes(row, frozen))
        collect_once(row, common, auth)
        wait_contest(row, "error", row["controller_health"]["deadline_seconds"], no_receipt=True)
        failed_row = submission_by_id(row, frozen["id"])
        if not failed_row or failed_row["judge_state"] != "permanent_failed" or failed_row["attempts"] <= frozen["attempts"]:
            raise AgentError("collection retry first failure was not persisted")
        remove_marker(marker)
        collect_once(row, common, auth)
        completed = wait_contest(row, "safe_wait", row["controller_health"]["deadline_seconds"])
        receipt_sha = verify_receipt(row, completed)
        delivered_row = wait_final_delivered(row, frozen, row["controller_health"]["deadline_seconds"])
        common_frozen = [{key: value for key, value in frozen.items() if key != "attempts"}]
        delivered = [{key: value for key, value in delivered_row.items() if key != "attempts"}]
        common.verify_unique_records(row, common_frozen, delivered)
        common.health_ready(row)
        if validate_mounts_and_config(row, common) != identity or common.ordinary_snapshot(row) != ordinary_before:
            raise AgentError("collection retry ordinary OJ or controller identity changed")
        ended = utc_now(); receipt = {"schema_version": 1, "qualification_marker": row["qualification_marker"],
            "session_id": row["session_id"], "failed_row_sha256": row["expected_failed_row_sha256"],
            "collection_receipt_sha256": receipt_sha, "started_at": started, "ended_at": ended}
        receipt_raw = canonical(receipt); common.atomic_write(Path(row["receipt_path"]), receipt_raw)
        action = {"$schema": "v1-fault-recovery-action-fact.schema.json", "schema_version": 1,
            "kind": "fault_recovery_action", "scenario": "collection_retry", "session_id": row["session_id"],
            "source": row["source"], "components": row["components"], "qualification_marker": row["qualification_marker"],
            "started_at": started, "ended_at": ended, "collector": {"mode": "trusted_action_agent", "agent_sha256": agent_sha},
            "signer": row["signer"], "signing_public_key": row["signing_public_key"],
            "payload": {"ordinary_oj_errors": 0, "ordinary_oj_restarts": 0, "ordinary_oj_pid_changes": 0,
                "duplicate_oj_records": 0, "final_source_mismatches": 0, "other_seat_failures": 0,
                "injected_failures": 1, "retry_attempts": 1, "successful_deliveries": 1, "collection_receipt_unique": True}}
        signature = common.sign(row, action); common.verify_signature(row, action, signature); action["signature"] = signature
        common.atomic_write(Path(row["output_path"]), canonical(action)); common.unlink_durable(state)
        return {"status": "passed", "collection_receipt_sha256": receipt_sha,
                "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest()}
    finally:
        remove_marker(marker)
        for signum, handler in handlers.items(): signal.signal(signum, handler)
        os.close(lock)


def main():
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0: raise AgentError("collection retry agent requires Linux root")
        if EMBEDDED_CONFIG is None: raise AgentError("collection retry agent is not frozen")
        print(json.dumps(run(EMBEDDED_CONFIG), sort_keys=True, separators=(",", ":"))); return 0
    except (AgentError, OSError, sqlite3.Error, subprocess.SubprocessError, yaml.YAMLError) as exc:
        print(f"NO_GO: {exc}", file=os.sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
