#!/usr/bin/env python3
"""Derive the 15-seat workload fact from signed actions, SQLite, and collection files."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import Any


EMBEDDED_CONFIG = None
NAMESPACE = "noi-v1-capacity-workload-actions"
HEX24 = re.compile(r"[a-f0-9]{24}")
HEX32 = re.compile(r"[a-f0-9]{32}")
HEX64 = re.compile(r"[a-f0-9]{64}")
MARKER = re.compile(r"NOI-V1-QUAL-[A-Z0-9]{16,64}")
SIGNER = re.compile(r"[A-Za-z0-9_.@+-]{1,80}")
SLUG = re.compile(r"[a-z][a-z0-9_]{0,31}")
SSH_PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?")
RID = re.compile(r"[a-f0-9]{24}")
CANDIDATE = re.compile(r"[0-9]{12}")


class WorkloadProbeError(RuntimeError):
    pass


def exact(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise WorkloadProbeError(f"{label} field set differs")
    return value


def absolute(value: object, label: str) -> str:
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or "\x00" in value:
        raise WorkloadProbeError(f"{label} must be an absolute path")
    if ".." in PurePosixPath(value).parts or "//" in value:
        raise WorkloadProbeError(f"{label} must be normalized")
    return value


def validate_config(value: object) -> dict:
    row = exact(value, {
        "schema_version", "qualification_marker", "seat_set_sha256", "database_path",
        "contest_id", "submission_session", "problem_slugs", "seat_bindings",
        "action_envelope", "action_receipt", "action_agent_sha256", "action_signer", "action_public_key", "action_max_age_seconds",
        "ssh_keygen_path", "capacity_session_dir",
    }, "workload probe configuration")
    if row["schema_version"] != 1 or not isinstance(row["qualification_marker"], str) or \
            not MARKER.fullmatch(row["qualification_marker"]):
        raise WorkloadProbeError("workload qualification marker is invalid")
    if not isinstance(row["seat_set_sha256"], str) or not HEX64.fullmatch(row["seat_set_sha256"]):
        raise WorkloadProbeError("workload seat set SHA256 is invalid")
    for key in ("database_path", "action_envelope", "action_receipt", "ssh_keygen_path",
                "capacity_session_dir"):
        row[key] = absolute(row[key], key)
    if len({row["database_path"], row["action_envelope"], row["action_receipt"],
            row["ssh_keygen_path"], row["capacity_session_dir"]}) != 5:
        raise WorkloadProbeError("workload private paths must differ")
    if not isinstance(row["contest_id"], str) or not HEX24.fullmatch(row["contest_id"]) or \
            not isinstance(row["submission_session"], str) or not HEX32.fullmatch(row["submission_session"]):
        raise WorkloadProbeError("workload contest identity is invalid")
    if not isinstance(row["action_agent_sha256"], str) or not HEX64.fullmatch(row["action_agent_sha256"]):
        raise WorkloadProbeError("workload action agent SHA256 is invalid")
    slugs = row["problem_slugs"]
    if not isinstance(slugs, list) or len(slugs) != 3 or len(set(slugs)) != 3 or \
            any(not isinstance(item, str) or not SLUG.fullmatch(item) for item in slugs):
        raise WorkloadProbeError("workload must bind exactly three problem slugs")
    row["problem_slugs"] = list(slugs)
    bindings = row["seat_bindings"]
    if not isinstance(bindings, list) or len(bindings) != 15:
        raise WorkloadProbeError("workload must bind exactly 15 formal seats")
    normalized = []
    for item in bindings:
        item = exact(item, {"slot_no", "uid", "candidate"}, "workload seat binding")
        if isinstance(item["slot_no"], bool) or not isinstance(item["slot_no"], int) or \
                not 1 <= item["slot_no"] <= 15 or isinstance(item["uid"], bool) or \
                not isinstance(item["uid"], int) or item["uid"] <= 0 or \
                not isinstance(item["candidate"], str) or not CANDIDATE.fullmatch(item["candidate"]):
            raise WorkloadProbeError("workload seat binding is invalid")
        normalized.append(dict(item))
    normalized.sort(key=lambda item: item["slot_no"])
    if [item["slot_no"] for item in normalized] != list(range(1, 16)) or \
            len({item["uid"] for item in normalized}) != 15 or \
            len({item["candidate"] for item in normalized}) != 15:
        raise WorkloadProbeError("workload seat bindings are not a unique 1..15 set")
    row["seat_bindings"] = normalized
    if not isinstance(row["action_signer"], str) or not SIGNER.fullmatch(row["action_signer"]) or \
            not isinstance(row["action_public_key"], str) or \
            not SSH_PUBLIC_KEY.fullmatch(row["action_public_key"]):
        raise WorkloadProbeError("workload action signer is invalid")
    age = row["action_max_age_seconds"]
    if isinstance(age, bool) or not isinstance(age, int) or not 1 <= age <= 7200:
        raise WorkloadProbeError("workload action maximum age is invalid")
    return row


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise WorkloadProbeError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkloadProbeError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise WorkloadProbeError(f"{label} is invalid")
    return parsed.astimezone(timezone.utc)


def safe_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or \
                stat.S_IMODE(info.st_mode) & 0o022:
            raise WorkloadProbeError("workload private path ancestor is unsafe")


def private_file(path: Path, label: str, *, executable: bool = False) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    info = resolved.stat(); safe_ancestors(resolved)
    if requested != resolved or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or \
            info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022 or \
            (executable and not os.access(resolved, os.X_OK)):
        raise WorkloadProbeError(f"{label} metadata is unsafe")
    return resolved


def private_directory(path: Path, label: str) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    info = resolved.stat(); safe_ancestors(resolved / "leaf")
    if requested != resolved or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or \
            (platform.system().lower() == "linux" and
             (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022)):
        raise WorkloadProbeError(f"{label} metadata is unsafe")
    return resolved


def read_bounded(path: Path, label: str, limit: int = 16 * 1024 * 1024) -> bytes:
    path = private_file(path, label)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) |
                         getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not 0 < info.st_size <= limit:
            raise WorkloadProbeError(f"{label} size is invalid")
        raw = os.read(descriptor, info.st_size + 1)
        if len(raw) != info.st_size:
            raise WorkloadProbeError(f"{label} changed while reading")
        return raw
    finally:
        os.close(descriptor)


def verify_sample_window(config: dict, started: datetime, completed: datetime) -> None:
    directory = private_directory(Path(config["capacity_session_dir"]), "capacity session directory")
    try:
        session = exact(json.loads(read_bounded(directory / "session.json", "capacity session").decode()), {
            "$schema", "schema_version", "session_id", "created_at", "source", "components",
            "environment", "thresholds", "probes", "duration_seconds", "sample_interval_seconds",
        }, "capacity session")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkloadProbeError("capacity session is not strict JSON") from exc
    duration, interval = session["duration_seconds"], session["sample_interval_seconds"]
    if session["$schema"] != "v1-capacity-session.schema.json" or session["schema_version"] != 1 or \
            not isinstance(session["session_id"], str) or not HEX64.fullmatch(session["session_id"]) or \
            isinstance(duration, bool) or not isinstance(duration, int) or duration < 3600 or \
            isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 60:
        raise WorkloadProbeError("capacity session identity differs")
    created = timestamp(session["created_at"], "capacity session created_at")
    samples_dir = private_directory(directory / "samples", "capacity sample directory")
    files = sorted(samples_dir.iterdir())
    planned = duration // interval + 1
    if len(files) != planned or [path.name for path in files] != \
            [f"{index:06d}.json" for index in range(1, planned + 1)]:
        raise WorkloadProbeError("capacity sample window is incomplete")
    bounds = []
    for expected_sequence, path in ((1, files[0]), (planned, files[-1])):
        try:
            sample = exact(json.loads(read_bounded(path, "capacity boundary sample").decode()), {
                "schema_version", "kind", "session_id", "sequence", "observed_at", "metrics",
                "telemetry", "ordinary_oj", "collector",
            }, "capacity boundary sample")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkloadProbeError("capacity boundary sample is not strict JSON") from exc
        ordinary = sample["ordinary_oj"]
        if sample["schema_version"] != 1 or sample["kind"] != "capacity_sample" or \
                sample["session_id"] != session["session_id"] or sample["sequence"] != expected_sequence or \
                not isinstance(ordinary, dict) or ordinary.get("qualification_marker") != config["qualification_marker"]:
            raise WorkloadProbeError("capacity boundary sample identity differs")
        bounds.append(timestamp(sample["observed_at"], "capacity boundary sample observed_at"))
    if not created <= bounds[0] <= started < completed <= bounds[1]:
        raise WorkloadProbeError("workload action occurred outside the capacity sample window")


def verify_action_envelope(config: dict, now: datetime) -> dict:
    raw = read_bounded(Path(config["action_envelope"]), "workload action envelope")
    try:
        envelope = exact(json.loads(raw.decode()), {
            "schema_version", "namespace", "signer", "payload", "signature_base64"
        }, "workload action envelope")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkloadProbeError("workload action envelope is not strict JSON") from exc
    if raw != canonical(envelope) or envelope["schema_version"] != 1 or \
            envelope["namespace"] != NAMESPACE or envelope["signer"] != config["action_signer"]:
        raise WorkloadProbeError("workload action envelope identity differs")
    payload = exact(envelope["payload"], {
        "schema_version", "qualification_marker", "seat_set_sha256", "contest_id_sha256",
        "observed_at", "operation_receipt_sha256", "login_slots", "material_open_slots", "compile_pairs",
    }, "workload action payload")
    if payload["schema_version"] != 1 or payload["qualification_marker"] != config["qualification_marker"] or \
            payload["seat_set_sha256"] != config["seat_set_sha256"] or \
            payload["contest_id_sha256"] != hashlib.sha256(config["contest_id"].encode()).hexdigest():
        raise WorkloadProbeError("workload action payload identity differs")
    observed = timestamp(payload["observed_at"], "workload action observed_at")
    age = (now - observed).total_seconds()
    if age < -5 or age > config["action_max_age_seconds"]:
        raise WorkloadProbeError("workload action payload is stale or future-dated")
    expected_slots = list(range(1, 16))
    for key in ("login_slots", "material_open_slots"):
        if payload[key] != expected_slots:
            raise WorkloadProbeError(f"workload {key} does not prove all 15 seats")
    compile_pairs = payload["compile_pairs"]
    expected_pairs = [
        {"slot_no": slot, "problem": problem}
        for slot in expected_slots for problem in config["problem_slugs"]
    ]
    if compile_pairs != expected_pairs:
        raise WorkloadProbeError("workload compile evidence does not prove 15 by 3 actions")
    receipt_raw = read_bounded(Path(config["action_receipt"]), "workload action receipt")
    try:
        receipt = exact(json.loads(receipt_raw.decode()), {
            "schema_version", "qualification_marker", "contest_id_sha256", "seat_set_sha256", "agent_sha256",
            "browser_envelope_sha256", "started_at", "completed_at", "seat_identities",
            "material_open_count", "compile_count", "compile_peak_concurrency",
        }, "workload action receipt")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkloadProbeError("workload action receipt is not strict JSON") from exc
    seats = receipt["seat_identities"]
    if receipt_raw != canonical(receipt) or hashlib.sha256(receipt_raw).hexdigest() != \
            payload["operation_receipt_sha256"] or receipt["schema_version"] != 1 or \
            receipt["qualification_marker"] != config["qualification_marker"] or \
            receipt["contest_id_sha256"] != payload["contest_id_sha256"] or \
            receipt["seat_set_sha256"] != config["seat_set_sha256"] or \
            receipt["agent_sha256"] != config["action_agent_sha256"] or \
            not isinstance(receipt["browser_envelope_sha256"], str) or \
            not HEX64.fullmatch(receipt["browser_envelope_sha256"]) or \
            not isinstance(seats, list) or len(seats) != 15 or \
            [item.get("slot_no") for item in seats if isinstance(item, dict)] != list(range(1, 16)) or \
            any(set(item) != {"slot_no", "candidate_sha256", "container_identity_sha256"} or
                item["candidate_sha256"] != hashlib.sha256(
                    next(binding["candidate"] for binding in config["seat_bindings"]
                         if binding["slot_no"] == item["slot_no"]).encode()
                ).hexdigest() or
                not isinstance(item["container_identity_sha256"], str) or
                not HEX64.fullmatch(item["container_identity_sha256"]) for item in seats) or \
            receipt["material_open_count"] != 15 or receipt["compile_count"] != 45 or \
            receipt["compile_peak_concurrency"] != 15:
        raise WorkloadProbeError("workload action receipt identity differs")
    started = timestamp(receipt["started_at"], "workload action receipt start")
    completed = timestamp(receipt["completed_at"], "workload action receipt completion")
    if not started < completed or receipt["completed_at"] != payload["observed_at"] or \
            completed - started > timedelta(minutes=30):
        raise WorkloadProbeError("workload action receipt timing differs")
    verify_sample_window(config, started, completed)
    signature_value = envelope["signature_base64"]
    if not isinstance(signature_value, str) or not re.fullmatch(r"[A-Za-z0-9+/=]{40,131072}", signature_value):
        raise WorkloadProbeError("workload action signature is invalid")
    try:
        signature_raw = base64.b64decode(signature_value, validate=True)
    except ValueError as exc:
        raise WorkloadProbeError("workload action signature is invalid") from exc
    binary = private_file(Path(config["ssh_keygen_path"]), "ssh-keygen", executable=True)
    with tempfile.TemporaryDirectory(prefix="noi-v1-workload-verify-") as temporary:
        allowed = Path(temporary) / "allowed_signers"; signature = Path(temporary) / "payload.sig"
        allowed.write_text(f"{config['action_signer']} {config['action_public_key']}\n")
        signature.write_bytes(signature_raw); os.chmod(allowed, 0o600); os.chmod(signature, 0o600)
        try:
            result = subprocess.run(
                [str(binary), "-Y", "verify", "-f", str(allowed), "-I", config["action_signer"],
                 "-n", NAMESPACE, "-s", str(signature)], input=canonical(payload),
                capture_output=True, check=False, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkloadProbeError("workload action signature verification could not complete") from exc
    if result.returncode:
        raise WorkloadProbeError("workload action signature is invalid")
    return payload


def connect_database(path: Path) -> sqlite3.Connection:
    path = private_file(path, "workload database")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        connection.close(); raise WorkloadProbeError("workload database integrity check failed")
    return connection


def sha256_file(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise WorkloadProbeError("collection evidence file could not be opened safely") from exc
    digest = hashlib.sha256()
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024 or \
                (platform.system().lower() == "linux" and
                 (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022)):
            raise WorkloadProbeError("collection evidence file metadata is unsafe")
        while True:
            block = os.read(descriptor, 65536)
            if not block: break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def read_collection_json(directory: Path, name: str) -> dict:
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise WorkloadProbeError(f"collection {name} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkloadProbeError(f"collection {name} is invalid") from exc
    if not isinstance(value, dict):
        raise WorkloadProbeError(f"collection {name} root differs")
    return value


def archive_manifest(directory: Path) -> dict:
    files: dict[str, dict[str, object]] = {}
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(directory).as_posix()
        if relative in {"archive_manifest.json", "collection_receipt.json"}:
            continue
        if path.is_symlink():
            raise WorkloadProbeError("workload collection tree contains a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise WorkloadProbeError("workload collection tree contains a special file")
        files[relative] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return {"schema_version": 1, "files": files}


def collect(config: dict, *, now: datetime | None = None) -> dict:
    current = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    actions = verify_action_envelope(config, current)
    connection = connect_database(Path(config["database_path"]))
    try:
        contest_rows = connection.execute("SELECT * FROM contests WHERE tid=?", (config["contest_id"],)).fetchall()
        if len(contest_rows) != 1:
            raise WorkloadProbeError("workload contest row is missing or duplicated")
        contest = dict(contest_rows[0])
        if contest.get("submission_session") != config["submission_session"] or \
                contest.get("state") not in {"safe_wait", "safe_ended", "done"}:
            raise WorkloadProbeError("workload contest run or terminal state differs")
        try:
            files = json.loads(contest.get("files") or "[]")
        except json.JSONDecodeError as exc:
            raise WorkloadProbeError("workload contest problem list is invalid") from exc
        if files != config["problem_slugs"]:
            raise WorkloadProbeError("workload contest problem list differs")
        seats = connection.execute(
            "SELECT uid,uname,candidate FROM seats WHERE tid=? ORDER BY uid", (config["contest_id"],)
        ).fetchall()
        expected_uids = sorted(item["uid"] for item in config["seat_bindings"])
        if [int(item["uid"]) for item in seats] != expected_uids:
            raise WorkloadProbeError("workload contest seat set differs")
        candidate_by_uid = {item["uid"]: item["candidate"] for item in config["seat_bindings"]}
        if any(str(item["candidate"]) != candidate_by_uid[int(item["uid"])] for item in seats):
            raise WorkloadProbeError("workload contest candidate binding differs")
        uid_by_uname = {str(item["uname"]): int(item["uid"]) for item in seats}
        if len(uid_by_uname) != 15:
            raise WorkloadProbeError("workload contest usernames are not unique")
        submissions = connection.execute(
            "SELECT uid,problem,judge_state,submission_id,rid,submission_session,judge_kind,"
            "sha256,judge_sha256 FROM web_submissions WHERE tid=? ORDER BY uid,problem,id",
            (config["contest_id"],),
        ).fetchall()
    finally:
        connection.close()
    expected_pairs = {(uid, problem) for uid in expected_uids for problem in config["problem_slugs"]}
    if len(submissions) != 45 or {(int(row["uid"]), str(row["problem"])) for row in submissions} != expected_pairs:
        raise WorkloadProbeError("workload database does not contain exactly one 15 by 3 submission set")
    for row in submissions:
        if row["judge_state"] != "submitted" or row["submission_session"] != config["submission_session"] or \
                row["judge_kind"] != "realtime" or not HEX64.fullmatch(str(row["submission_id"])) or \
                not RID.fullmatch(str(row["rid"])) or not HEX64.fullmatch(str(row["sha256"])) or \
                row["judge_sha256"] != row["sha256"]:
            raise WorkloadProbeError("workload submission is incomplete or not delivered")
    if len({str(row["submission_id"]) for row in submissions}) != 45 or \
            len({str(row["rid"]) for row in submissions}) != 45:
        raise WorkloadProbeError("workload submission or OJ record identity was reused")
    submission_by_pair = {
        (int(row["uid"]), str(row["problem"])): dict(row) for row in submissions
    }
    directory = private_directory(
        Path(str(contest.get("collection_dir") or "")), "workload collection directory"
    )
    receipt = read_collection_json(directory, "collection_receipt.json")
    expected_files = {"archive_manifest.json", "folder_report.json", "web_report.json", "selection.json", "report.json", "submit_log.json"}
    if exact(receipt, {"schema_version", "tid", "run_id", "completed_at_ms", "cutoff_at_ms", "shutdown_after_ms", "seat_count", "problem_count", "submit_failures", "files"}, "collection receipt")["schema_version"] != 1 or \
            receipt["tid"] != config["contest_id"] or receipt["run_id"] != contest.get("collection_run_id") or \
            receipt["seat_count"] != 15 or receipt["problem_count"] != 3 or receipt["submit_failures"] != 0 or \
            not isinstance(receipt["files"], dict) or set(receipt["files"]) != expected_files or \
            sha256_file(directory / "collection_receipt.json") != contest.get("collection_receipt_sha256"):
        raise WorkloadProbeError("workload collection receipt differs")
    for name, digest in receipt["files"].items():
        if not isinstance(digest, str) or not HEX64.fullmatch(digest) or sha256_file(directory / name) != digest:
            raise WorkloadProbeError("workload collection artifact digest differs")
    if read_collection_json(directory, "archive_manifest.json") != archive_manifest(directory):
        raise WorkloadProbeError("workload collection tree differs from its frozen manifest")
    report = read_collection_json(directory, "report.json")
    submit_log = read_collection_json(directory, "submit_log.json")
    if len(report) != 15 or set(report) != set(submit_log):
        raise WorkloadProbeError("workload collection participant set differs")
    delivered = 0
    for uname, problems in report.items():
        if uname not in uid_by_uname:
            raise WorkloadProbeError("workload collection username is not a frozen seat")
        if not isinstance(problems, dict) or list(problems) != config["problem_slugs"]:
            raise WorkloadProbeError("workload final report problem set differs")
        logs = submit_log.get(uname)
        if not isinstance(logs, dict) or set(logs) != set(config["problem_slugs"]):
            raise WorkloadProbeError("workload submit log problem set differs")
        slot_no = next(
            item["slot_no"] for item in config["seat_bindings"]
            if item["uid"] == uid_by_uname[uname]
        )
        for problem, item in problems.items():
            submission = submission_by_pair[(uid_by_uname[uname], problem)]
            relative = PurePosixPath(str(item.get("file") or "")) if isinstance(item, dict) else PurePosixPath("")
            if relative.is_absolute() or len(relative.parts) != 3 or ".." in relative.parts or \
                    not CANDIDATE.fullmatch(relative.parts[0]) or relative.parts[1] != problem or \
                    relative.parts[2] != f"{problem}.cpp":
                raise WorkloadProbeError("workload final source path differs from the CSP contract")
            frozen_source = directory / "slots" / f"{slot_no:03d}" / "answers" / Path(*relative.parts)
            if not isinstance(item, dict) or item.get("status") != "ok" or \
                    item.get("submission_source") != "confirmed_submit" or \
                    item.get("reuses_confirmed_submission") is not True or \
                    item.get("sha256") != submission["sha256"] or \
                    sha256_file(frozen_source) != submission["sha256"]:
                raise WorkloadProbeError("workload final source differs from confirmed submission")
            log = logs[problem]
            if not isinstance(log, dict) or log.get("ok") is not True or \
                    log.get("reused_realtime") is not True or log.get("rid") != submission["rid"]:
                raise WorkloadProbeError("workload collection delivery receipt differs")
            delivered += 1
    if delivered != 45:
        raise WorkloadProbeError("workload collection delivery count differs")
    return {
        "observed_at": current.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "login_successes": len(actions["login_slots"]),
        "material_open_successes": len(actions["material_open_slots"]),
        "compile_successes": len(actions["compile_pairs"]),
        "submission_successes": len(submissions),
        "failed_submissions": 0,
        "collection_successes": int(receipt["seat_count"]),
        "failed_collections": 0,
        "final_source_mismatches": 0,
    }


def main() -> int:
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise WorkloadProbeError("capacity workload probe requires Linux root")
        config = validate_config(EMBEDDED_CONFIG)
        print(json.dumps(collect(config), sort_keys=True, separators=(",", ":")))
        return 0
    except (WorkloadProbeError, OSError, sqlite3.Error) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
