#!/usr/bin/env python3
"""Execute the frozen 15-seat material/compile workload and sign a privacy-safe fact."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time


EMBEDDED_CONFIG = None
NAMESPACE = "noi-v1-capacity-workload-actions"
BROWSER_NAMESPACE = "noi-v1-capacity-telemetry"
HEX24 = re.compile(r"[a-f0-9]{24}")
HEX64 = re.compile(r"[a-f0-9]{64}")
MARKER = re.compile(r"NOI-V1-QUAL-[A-Z0-9]{16,64}")
SIGNER = re.compile(r"[A-Za-z0-9_.@+-]{1,80}")
PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?")
SLUG = re.compile(r"[a-z][a-z0-9_]{0,31}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.]\d+)?Z")


class ActionAgentError(RuntimeError):
    pass


def exact(value, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ActionAgentError(f"{label} field set differs")
    return value


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ActionAgentError(f"{label} is invalid")
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ActionAgentError(f"{label} is invalid") from exc
    return parsed.astimezone(timezone.utc)


def absolute(value, label: str) -> str:
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or "\x00" in value or \
            ".." in PurePosixPath(value).parts or "//" in value:
        raise ActionAgentError(f"{label} must be a normalized absolute path")
    return value


def validate_config(value) -> dict:
    row = exact(value, {
        "schema_version", "qualification_marker", "contest_id", "seat_set_sha256",
        "problem_slugs", "seats", "docker_path", "docker_socket", "browser_envelope", "browser_signer",
        "browser_public_key", "browser_max_age_seconds", "signer", "signing_public_key",
        "signing_key_path", "ssh_keygen_path", "lock_path", "receipt_path", "output_path",
    }, "workload action agent configuration")
    if row["schema_version"] != 1 or not isinstance(row["qualification_marker"], str) or \
            not MARKER.fullmatch(row["qualification_marker"]) or \
            not isinstance(row["contest_id"], str) or not HEX24.fullmatch(row["contest_id"]) or \
            not isinstance(row["seat_set_sha256"], str) or not HEX64.fullmatch(row["seat_set_sha256"]) or \
            row["docker_socket"] != "/var/run/docker.sock":
        raise ActionAgentError("workload action identity is invalid")
    slugs = row["problem_slugs"]
    if not isinstance(slugs, list) or len(slugs) != 3 or len(set(slugs)) != 3 or \
            any(not isinstance(item, str) or not SLUG.fullmatch(item) for item in slugs):
        raise ActionAgentError("workload action problems differ")
    seats = row["seats"]
    if not isinstance(seats, list) or len(seats) != 15:
        raise ActionAgentError("workload action requires 15 seats")
    normalized = []
    for seat in seats:
        seat = exact(seat, {"slot_no", "candidate", "container_id", "container_name", "image_id",
                            "pid", "started_at", "restart_count"}, "workload action seat")
        if isinstance(seat["slot_no"], bool) or not isinstance(seat["slot_no"], int) or \
                not 1 <= seat["slot_no"] <= 15 or not isinstance(seat["candidate"], str) or \
                not re.fullmatch(r"[0-9]{12}", seat["candidate"]) or \
                not isinstance(seat["container_id"], str) or not HEX64.fullmatch(seat["container_id"]) or \
                not isinstance(seat["container_name"], str) or \
                not re.fullmatch(r"seat-[a-f0-9]{8}-slot-[0-9]{3}", seat["container_name"]) or \
                not isinstance(seat["image_id"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", seat["image_id"]) or \
                isinstance(seat["pid"], bool) or not isinstance(seat["pid"], int) or seat["pid"] <= 0 or \
                not isinstance(seat["started_at"], str) or not TIMESTAMP.fullmatch(seat["started_at"]) or \
                isinstance(seat["restart_count"], bool) or not isinstance(seat["restart_count"], int) or \
                seat["restart_count"] < 0:
            raise ActionAgentError("workload action seat identity is invalid")
        normalized.append(dict(seat))
    normalized.sort(key=lambda item: item["slot_no"])
    if [item["slot_no"] for item in normalized] != list(range(1, 16)) or \
            len({item["candidate"] for item in normalized}) != 15 or \
            len({item["container_id"] for item in normalized}) != 15 or \
            len({item["container_name"] for item in normalized}) != 15:
        raise ActionAgentError("workload action seat set is not unique")
    row["seats"] = normalized
    for key in ("docker_path", "browser_envelope", "signing_key_path", "ssh_keygen_path",
                "lock_path", "receipt_path", "output_path"):
        row[key] = absolute(row[key], key)
    if len({row[key] for key in ("browser_envelope", "signing_key_path", "lock_path",
                                  "receipt_path", "output_path")}) != 5 or \
            any(not isinstance(row[key], str) or not SIGNER.fullmatch(row[key])
                for key in ("browser_signer", "signer")) or \
            any(not isinstance(row[key], str) or not PUBLIC_KEY.fullmatch(row[key])
                for key in ("browser_public_key", "signing_public_key")) or \
            isinstance(row["browser_max_age_seconds"], bool) or \
            not isinstance(row["browser_max_age_seconds"], int) or \
            not 5 <= row["browser_max_age_seconds"] <= 120:
        raise ActionAgentError("workload action private identity differs")
    return row


def safe_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part; info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or \
                stat.S_IMODE(info.st_mode) & 0o022:
            raise ActionAgentError("workload action private path ancestor is unsafe")


def regular(path: Path, label: str, *, private=False, executable=False) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    info = resolved.stat(); safe_ancestors(resolved)
    if requested != resolved or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or \
            info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022) or \
            (executable and not os.access(resolved, os.X_OK)):
        raise ActionAgentError(f"{label} metadata is unsafe")
    return resolved


def read_private(path: Path, label: str, limit=4 * 1024 * 1024) -> bytes:
    path = regular(path, label, private=True)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not 0 < info.st_size <= limit: raise ActionAgentError(f"{label} size is invalid")
        raw = os.read(descriptor, info.st_size + 1)
        if len(raw) != info.st_size: raise ActionAgentError(f"{label} changed while reading")
        return raw
    finally: os.close(descriptor)


def atomic_write(path: Path, raw: bytes) -> None:
    parent = path.parent.resolve(strict=True); safe_ancestors(path); info = parent.stat()
    if parent != path.parent or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise ActionAgentError("workload action output parent is unsafe")
    descriptor, name = tempfile.mkstemp(prefix=".workload-action-", dir=parent); temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass


def file_sha256(path: Path, label: str) -> str:
    path = regular(path, label, executable=True)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)); digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        while True:
            block = os.read(descriptor, 65536)
            if not block: break
            digest.update(block)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != \
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ActionAgentError(f"{label} changed while hashing")
    finally: os.close(descriptor)
    return digest.hexdigest()


def run_command(command: list[str], label: str, *, input_bytes=None, timeout=30, ok=(0,)) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, input=input_bytes, capture_output=True, check=False, timeout=timeout,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActionAgentError(f"{label} could not complete") from exc
    if result.returncode not in ok: raise ActionAgentError(f"{label} failed")
    return result


def verify_signature(config: dict, payload: dict, signature_value: object, signer: str,
                     public_key: str, namespace: str) -> None:
    if not isinstance(signature_value, str) or not re.fullmatch(r"[A-Za-z0-9+/=]{40,131072}", signature_value):
        raise ActionAgentError("workload action signature encoding is invalid")
    try: signature_raw = base64.b64decode(signature_value, validate=True)
    except ValueError as exc: raise ActionAgentError("workload action signature encoding is invalid") from exc
    binary = regular(Path(config["ssh_keygen_path"]), "ssh-keygen", executable=True)
    with tempfile.TemporaryDirectory(prefix="noi-v1-workload-verify-") as directory:
        allowed = Path(directory) / "allowed_signers"; signature = Path(directory) / "payload.sig"
        allowed.write_text(f"{signer} {public_key}\n"); signature.write_bytes(signature_raw)
        os.chmod(allowed, 0o600); os.chmod(signature, 0o600)
        result = run_command([str(binary), "-Y", "verify", "-f", str(allowed), "-I", signer,
                              "-n", namespace, "-s", str(signature)], "workload action signature verification",
                             input_bytes=canonical(payload), timeout=10)
    if result.returncode: raise ActionAgentError("workload action signature differs")


def sign(config: dict, payload: dict) -> str:
    key = regular(Path(config["signing_key_path"]), "workload action signing key", private=True)
    binary = regular(Path(config["ssh_keygen_path"]), "ssh-keygen", executable=True)
    with tempfile.TemporaryDirectory(prefix="noi-v1-workload-sign-") as directory:
        source = Path(directory) / "payload.json"; source.write_bytes(canonical(payload)); os.chmod(source, 0o600)
        run_command([str(binary), "-q", "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(source)],
                    "workload action signing", timeout=10)
        signature = Path(str(source) + ".sig")
        if not signature.is_file(): raise ActionAgentError("workload action signature is missing")
        return base64.b64encode(signature.read_bytes()).decode()


def verify_browser(config: dict, now: datetime) -> str:
    raw = read_private(Path(config["browser_envelope"]), "browser telemetry envelope")
    try: envelope = exact(json.loads(raw.decode()), {"schema_version", "namespace", "signer", "payload", "signature_base64"}, "browser telemetry envelope")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ActionAgentError("browser telemetry is not strict JSON") from exc
    if raw != canonical(envelope) or envelope["schema_version"] != 1 or \
            envelope["namespace"] != BROWSER_NAMESPACE or envelope["signer"] != config["browser_signer"]:
        raise ActionAgentError("browser telemetry identity differs")
    payload = exact(envelope["payload"], {"schema_version", "transport_profile", "qualification_marker",
                    "seat_set_sha256", "formal_seat_count", "sequence", "window_started_at", "observed_at",
                    "rtt_samples_ms", "packet_loss_percent", "websocket_reconnects", "key_to_frame_samples_ms"},
                    "browser telemetry payload")
    started = parse_time(payload["window_started_at"], "browser window_started_at")
    observed = parse_time(payload["observed_at"], "browser observed_at")
    age = (now - observed).total_seconds()
    if payload["schema_version"] != 1 or payload["qualification_marker"] != config["qualification_marker"] or \
            payload["seat_set_sha256"] != config["seat_set_sha256"] or payload["formal_seat_count"] != 15 or \
            payload["transport_profile"] not in {"direct_http", "compat_https"} or \
            isinstance(payload["sequence"], bool) or not isinstance(payload["sequence"], int) or \
            payload["sequence"] < 1 or not started < observed or (observed - started).total_seconds() > 60 or \
            not isinstance(payload["rtt_samples_ms"], list) or len(payload["rtt_samples_ms"]) < 5 or \
            not isinstance(payload["key_to_frame_samples_ms"], list) or len(payload["key_to_frame_samples_ms"]) < 5 or \
            any(isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
                for key in ("rtt_samples_ms", "key_to_frame_samples_ms") for value in payload[key]) or \
            isinstance(payload["packet_loss_percent"], bool) or \
            not isinstance(payload["packet_loss_percent"], (int, float)) or \
            not 0 <= payload["packet_loss_percent"] <= 100 or \
            isinstance(payload["websocket_reconnects"], bool) or \
            not isinstance(payload["websocket_reconnects"], int) or payload["websocket_reconnects"] < 0 or \
            age < -5 or age > config["browser_max_age_seconds"]:
        raise ActionAgentError("browser telemetry does not prove current 15-seat login")
    verify_signature(config, payload, envelope["signature_base64"], config["browser_signer"],
                     config["browser_public_key"], BROWSER_NAMESPACE)
    return hashlib.sha256(raw).hexdigest()


def inspect_seat(config: dict, seat: dict) -> None:
    docker = str(regular(Path(config["docker_path"]), "docker", executable=True))
    raw = run_command([docker, "--host", f"unix://{config['docker_socket']}", "inspect", seat["container_id"]],
                      "workload seat inspect").stdout
    try: values = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ActionAgentError("workload seat inspect is not JSON") from exc
    if not isinstance(values, list) or len(values) != 1: raise ActionAgentError("workload seat inspect count differs")
    value = values[0]; state = value.get("State") or {}
    observed = {"container_id": value.get("Id"), "container_name": str(value.get("Name") or "").lstrip("/"),
                "image_id": value.get("Image"), "pid": state.get("Pid"), "started_at": state.get("StartedAt"),
                "restart_count": value.get("RestartCount")}
    if observed != {key: seat[key] for key in observed} or state.get("Running") is not True or \
            state.get("Restarting") is not False:
        raise ActionAgentError("workload seat lifecycle changed")


def execute_actions(config: dict) -> tuple[list[int], list[dict], int]:
    docker = str(regular(Path(config["docker_path"]), "docker", executable=True))
    for seat in config["seats"]:
        inspect_seat(config, seat)
        for problem in config["problem_slugs"]:
            directory = f"/home/student/答案/{seat['candidate']}/{problem}"
            run_command([docker, "--host", f"unix://{config['docker_socket']}", "exec", "-u", "student", "-w", directory, seat["container_id"],
                         "/usr/bin/test", "-r", f"{problem}.cpp"], "workload source preflight")
    start = threading.Barrier(15); compile_barriers = [threading.Barrier(15) for _ in config["problem_slugs"]]
    interval_lock = threading.Lock(); intervals: list[tuple[int, float, float]] = []
    def wait_barrier(barrier: threading.Barrier, label: str) -> None:
        try: barrier.wait(timeout=30)
        except threading.BrokenBarrierError as exc: raise ActionAgentError(f"{label} did not reach 15 seats") from exc
    def seat_work(seat: dict) -> tuple[int, list[dict]]:
        run_command([docker, "--host", f"unix://{config['docker_socket']}", "exec", "-u", "student", "-e", "HOME=/home/student", "-e", "DISPLAY=:1",
                     seat["container_id"], "/usr/bin/timeout", "--signal=TERM", "3",
                     "/usr/bin/evince", "--preview", "/run/contest-materials/01_比赛题面.pdf"],
                    "workload material open", timeout=10, ok=(0, 124))
        wait_barrier(start, "workload action start barrier"); pairs = []
        for problem_index, problem in enumerate(config["problem_slugs"]):
            directory = f"/home/student/答案/{seat['candidate']}/{problem}"
            output = f"/tmp/noi-v1-qual-{config['qualification_marker']}-{problem}"
            try:
                wait_barrier(compile_barriers[problem_index], "workload compile barrier")
                compile_started = time.monotonic()
                run_command([docker, "--host", f"unix://{config['docker_socket']}", "exec", "-u", "student", "-w", directory, seat["container_id"],
                             "/usr/bin/g++", "-std=c++14", "-O2", "-pipe", f"{problem}.cpp", "-o", output],
                            "workload source compile", timeout=60)
                compile_completed = time.monotonic()
                with interval_lock: intervals.append((problem_index, compile_started, compile_completed))
                pairs.append({"slot_no": seat["slot_no"], "problem": problem})
            finally:
                run_command([docker, "--host", f"unix://{config['docker_socket']}", "exec", "-u", "student", seat["container_id"], "/bin/rm", "-f", output],
                            "workload compile cleanup")
        inspect_seat(config, seat); return seat["slot_no"], pairs
    with ThreadPoolExecutor(max_workers=15, thread_name_prefix="noi-v1-seat") as executor:
        results = list(executor.map(seat_work, config["seats"]))
    material_slots = sorted(slot for slot, _ in results)
    compile_pairs = sorted((pair for _, pairs in results for pair in pairs),
                           key=lambda item: (item["slot_no"], config["problem_slugs"].index(item["problem"])))
    peak = 0
    for problem_index in range(3):
        events = sorted((moment, delta) for index, started, completed in intervals
                        if index == problem_index for moment, delta in ((started, 1), (completed, -1)))
        active = 0
        for _, delta in events:
            active += delta; peak = max(peak, active)
    if peak != 15: raise ActionAgentError("workload compile concurrency did not reach 15 seats")
    return material_slots, compile_pairs, peak


def acquire_lock(path: Path) -> int:
    import fcntl
    safe_ancestors(path); descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or \
                stat.S_IMODE(info.st_mode) & 0o077:
            raise ActionAgentError("workload action lock metadata is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB); return descriptor
    except BaseException: os.close(descriptor); raise


def run(config: dict, *, runtime_path: Path | None = None) -> dict:
    row = validate_config(config)
    agent_sha = file_sha256(Path(__file__) if runtime_path is None else runtime_path,
                            "workload frozen action agent")
    for path in (Path(row["receipt_path"]), Path(row["output_path"])):
        if os.path.lexists(path): raise ActionAgentError("workload action output already exists")
    lock = acquire_lock(Path(row["lock_path"])); prior = {}
    def interrupted(signum, _frame): raise ActionAgentError(f"workload action interrupted by signal {signum}")
    try:
        for signum in tuple(x for x in (getattr(signal, "SIGHUP", None), signal.SIGINT, signal.SIGTERM) if x is not None):
            prior[signum] = signal.getsignal(signum); signal.signal(signum, interrupted)
        preflight = {"schema_version": 1, "qualification_marker": row["qualification_marker"], "purpose": "preflight-signature-check"}
        preflight_signature = sign(row, preflight)
        verify_signature(row, preflight, preflight_signature, row["signer"], row["signing_public_key"], NAMESPACE)
        started = utc_now(); browser_sha = verify_browser(row, datetime.now(timezone.utc))
        material_slots, compile_pairs, compile_peak = execute_actions(row); completed = utc_now()
        identities = [{"slot_no": seat["slot_no"],
                       "candidate_sha256": hashlib.sha256(seat["candidate"].encode()).hexdigest(),
                       "container_identity_sha256": hashlib.sha256(canonical({
            key: seat[key] for key in ("container_id", "container_name", "image_id", "pid", "started_at", "restart_count")
        })).hexdigest()} for seat in row["seats"]]
        receipt = {"schema_version": 1, "qualification_marker": row["qualification_marker"],
                   "contest_id_sha256": hashlib.sha256(row["contest_id"].encode()).hexdigest(),
                   "seat_set_sha256": row["seat_set_sha256"], "browser_envelope_sha256": browser_sha,
                   "agent_sha256": agent_sha,
                   "started_at": started, "completed_at": completed, "seat_identities": identities,
                   "material_open_count": len(material_slots), "compile_count": len(compile_pairs),
                   "compile_peak_concurrency": compile_peak}
        receipt_raw = canonical(receipt); atomic_write(Path(row["receipt_path"]), receipt_raw)
        payload = {"schema_version": 1, "qualification_marker": row["qualification_marker"],
                   "seat_set_sha256": row["seat_set_sha256"],
                   "contest_id_sha256": receipt["contest_id_sha256"], "observed_at": completed,
                   "operation_receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
                   "login_slots": list(range(1, 16)), "material_open_slots": material_slots,
                   "compile_pairs": compile_pairs}
        signature = sign(row, payload)
        verify_signature(row, payload, signature, row["signer"], row["signing_public_key"], NAMESPACE)
        atomic_write(Path(row["output_path"]), canonical({"schema_version": 1, "namespace": NAMESPACE,
                     "signer": row["signer"], "payload": payload, "signature_base64": signature}))
        return {"status": "passed", "receipt_sha256": payload["operation_receipt_sha256"]}
    finally:
        for signum, handler in prior.items(): signal.signal(signum, handler)
        os.close(lock)


def main() -> int:
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise ActionAgentError("workload action agent requires Linux root")
        if EMBEDDED_CONFIG is None: raise ActionAgentError("workload action agent is not frozen")
        print(json.dumps(run(EMBEDDED_CONFIG), sort_keys=True, separators=(",", ":"))); return 0
    except (ActionAgentError, OSError, json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
