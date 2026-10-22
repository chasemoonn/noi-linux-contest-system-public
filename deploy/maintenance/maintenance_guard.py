#!/usr/bin/env python3
"""Bounded, host-local APT/dpkg exclusion for a dedicated contest VM.

Candidate: install/start only in an approved maintenance window. No package
transactions, timer masks, apt configuration edits, or cloud actions here.
The existing APT/dpkg lock inodes are never truncated, replaced or deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # Windows can run policy tests, never the host guard.
    fcntl = None

SCHEMA = 1
MAX_WINDOW_MS = 4 * 60 * 60 * 1000
CLOSE_RESERVE_MS = 45_000
LOCK_PATHS = (
    "/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock",
    "/var/lib/apt/lists/lock", "/var/cache/apt/archives/lock",
)
RUNTIME = Path("/run/noi-maintenance")
CONFIG = Path("/etc/noi-maintenance/lease.json")
UNITS = ("apt-daily.service", "apt-daily-upgrade.service",
         "apt-daily.timer", "apt-daily-upgrade.timer")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}


class GuardError(RuntimeError):
    pass


def now_ms():
    return int(time.time() * 1000)


def boot_seconds():
    # Unlike monotonic on Linux, BOOTTIME includes suspend. Clock rollback
    # cannot prolong the lease, nor can suspend suspend its expiry.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def integer(value):
    if type(value) is not int:
        raise GuardError("integer required")
    return value


def validate_lease(value, *, machine_id, boot_id, now):
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
        raise GuardError("unsupported lease schema")
    if value.get("dedicated_contest_host") is not True:
        raise GuardError("dedicated contest host approval missing")
    if not re.fullmatch(r"[0-9a-f]{32}", str(value.get("machine_id", ""))):
        raise GuardError("invalid machine id")
    if value["machine_id"] != machine_id or value.get("boot_id") != boot_id:
        raise GuardError("wrong host or boot; a reboot needs fresh approval")
    if not re.fullmatch(r"[0-9a-f]{24}", str(value.get("tid", ""))):
        raise GuardError("invalid fresh contest id")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(value.get("approved_by", ""))):
        raise GuardError("responsible owner missing")
    start = integer(value.get("window_start_ms"))
    end = integer(value.get("window_end_ms"))
    reviewed = integer(value.get("maintenance_reviewed_at_ms"))
    if not 0 < end - start <= MAX_WINDOW_MS or not start <= now < end - 180_000:
        raise GuardError("outside approved bounded window")
    if not start <= reviewed <= now:
        raise GuardError("pre-race update review must be within this window")
    if value.get("maintenance_review_complete") is not True:
        raise GuardError("pre-race update and trigger review not confirmed")
    return dict(value)


class Deadline:
    def __init__(self, lease, *, wall, boot):
        self.expires = lease["window_end_ms"] - CLOSE_RESERVE_MS
        self.boot_expires = boot + (self.expires - wall) / 1000

    def remaining_ms(self, *, wall, boot):
        return max(0, min(self.expires - wall, int((self.boot_expires - boot) * 1000)))


def secure_regular(path):
    # Check the entire lexical chain, including root-owned parents. Do not
    # follow symlinks even if their resolved target looks safe.
    for item in (path, *path.parents):
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise GuardError("unsafe root-owned path")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise GuardError("not a single-link regular file")


class PackageLocks:
    """POSIX record locks, compatible with APT/dpkg (NOT BSD flock)."""
    def __init__(self, paths=LOCK_PATHS):
        self.paths = tuple(Path(p) for p in paths)
        self.handles = []

    def acquire(self):
        if fcntl is None or sys.platform != "linux":
            raise GuardError("package locks require Linux")
        try:
            for path in self.paths:
                # No O_CREAT: a missing/unexpected standard path fails closed.
                descriptor = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
                self.handles.append((path, descriptor))
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise GuardError("unsafe package lock file")
                fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.verify()
        except Exception:
            self.release()
            raise

    def verify(self):
        if len(self.handles) != len(self.paths):
            raise GuardError("package lock set incomplete")
        for path, descriptor in self.handles:
            actual, held = path.lstat(), os.fstat(descriptor)
            if (actual.st_dev, actual.st_ino, actual.st_nlink) != (held.st_dev, held.st_ino, 1):
                raise GuardError("package lock inode replaced")

    def release(self):
        while self.handles:
            _, descriptor = self.handles.pop()
            os.close(descriptor)  # Kernel releases the record lock. NEVER unlink.


def command(*args, timeout=10):
    result = subprocess.run(args, env=ENV, capture_output=True, text=True,
                            timeout=timeout, check=False, close_fds=True)
    if result.returncode:
        # Do not log arbitrary command output, which can contain private data.
        raise GuardError("host prerequisite command failed: " + args[0])
    return result.stdout.strip()


def unit_state(unit):
    text = command("/usr/bin/systemctl", "show", unit,
                   "--property=LoadState,ActiveState,SubState,UnitFileState,ControlPID")
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def preflight():
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                   if "=" in line)
    if release.get("ID", "").strip('"') != "ubuntu" or release.get("VERSION_ID", "").strip('"') != "22.04":
        raise GuardError("candidate is restricted to Ubuntu 22.04")
    if Path("/run/reboot-required").exists():
        raise GuardError("pending reboot; complete maintenance before contest")
    if command("/usr/bin/dpkg", "--audit"):
        raise GuardError("dpkg audit is not clean")
    # Resolve apt's actual paths; administrator overrides must not silently
    # bypass the four standard locks this guard owns.
    expected = {"ROOT": "/", "STATUS": "/var/lib/dpkg/status",
                "LISTS": "/var/lib/apt/lists/", "ARCHIVES": "/var/cache/apt/archives/"}
    output = command("/usr/bin/apt-config", "shell", "ROOT", "Dir/d",
                     "STATUS", "Dir::State::status/f", "LISTS", "Dir::State::lists/d",
                     "ARCHIVES", "Dir::Cache::archives/d", "NOLOCK", "Debug::NoLocking/b")
    values = dict(re.findall(r"^([A-Z]+)='([^']*)'$", output, re.M))
    if any(values.get(key) != val for key, val in expected.items()) or values.get("NOLOCK", "false") != "false":
        raise GuardError("nonstandard APT lock configuration")
    units = {unit: unit_state(unit) for unit in UNITS}
    for unit, state in units.items():
        if state.get("LoadState") not in {"loaded", "masked"}:
            raise GuardError("unknown update trigger unit")
        if unit.endswith(".service") and state.get("ActiveState") != "inactive":
            raise GuardError("update service is not idle; wait without killing it")
    for name in ("nginx.service", "docker.service"):
        state = unit_state(name)
        if state.get("LoadState") != "loaded" or state.get("ActiveState") != "active" or state.get("ControlPID") != "0":
            raise GuardError("critical service is not stable")
    return {"os": "ubuntu-22.04", "units": units, "apt_paths": values}


def close_gateway():
    # Only the pinned, dedicated contest host. Do not stop Docker or student
    # containers: archive files and their filesystem stay available.
    command("/usr/bin/systemctl", "stop", "nginx.service", timeout=20)
    state = unit_state("nginx.service")
    if state.get("ActiveState") != "inactive" or state.get("ControlPID") != "0":
        raise GuardError("gateway closure not confirmed")


def audit(event, **fields):
    print(json.dumps({"schema_version": SCHEMA, "event": event, "at_ms": now_ms(), **fields},
                     sort_keys=True), flush=True)


def notify(message):
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address or address[0] not in {"/", "@"}:
        raise GuardError("systemd notification socket missing")
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.settimeout(1)
        channel.sendto(message.encode(), address)


def verify_supervisor():
    state = dict(line.split("=", 1) for line in command(
        "/usr/bin/systemctl", "show", "noi-maintenance-guard.service",
        "--property=MainPID,Type,WatchdogUSec,RuntimeMaxUSec,Restart,InvocationID"
    ).splitlines() if "=" in line)
    if (state.get("MainPID") != str(os.getpid()) or state.get("Type") != "notify"
            or state.get("WatchdogUSec") != "15s" or state.get("RuntimeMaxUSec") != "4h"
            or state.get("Restart") != "no"
            or state.get("InvocationID") != os.environ.get("INVOCATION_ID")):
        raise GuardError("unreviewed supervisor configuration")


class Guard:
    def __init__(self, lease, locks, *, wall=now_ms, boot=boot_seconds, close=close_gateway):
        self.lease, self.locks = lease, locks
        self.wall, self.boot, self.close = wall, boot, close
        self.deadline = Deadline(lease, wall=wall(), boot=boot())
        self.active = True

    def status(self, nonce):
        self.locks.verify()
        remaining = self.deadline.remaining_ms(wall=self.wall(), boot=self.boot())
        if not self.active or remaining <= 0:
            raise GuardError("maintenance lease expired or closing")
        return {"schema_version": SCHEMA, "state": "protected", "nonce": nonce,
                "machine_id": self.lease["machine_id"], "boot_id": self.lease["boot_id"],
                "tid": self.lease["tid"], "pid": os.getpid(), "checked_at_ms": self.wall(),
                "expires_at_ms": self.deadline.expires, "remaining_ms": remaining,
                "locks": list(LOCK_PATHS)}

    def finish(self):
        # Closing succeeds before unlock in the ordinary path. An exceptional
        # failure MUST NOT turn into a permanent suspension of security updates.
        self.active = False
        try:
            self.close()
        finally:
            self.locks.release()


def status_request(data, guard):
    if len(data) > 1024:
        raise GuardError("oversized request")
    request = json.loads(data)
    if not isinstance(request, dict):
        raise GuardError("invalid request")
    nonce = request.get("nonce", "")
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce) or request.get("command") != "status":
        raise GuardError("unsupported request")
    return guard.status(nonce)


def read_lease():
    secure_regular(CONFIG)
    if CONFIG.stat().st_size > 8192:
        raise GuardError("lease manifest too large")
    return validate_lease(json.loads(CONFIG.read_text()),
                          machine_id=Path("/etc/machine-id").read_text().strip(),
                          boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(), now=now_ms())


def serve():
    if os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{32}", os.environ.get("INVOCATION_ID", "")):
        raise GuardError("run only through the reviewed root systemd unit")
    lease = read_lease()
    verify_supervisor()
    runtime_stat = RUNTIME.lstat()
    if not stat.S_ISDIR(runtime_stat.st_mode) or runtime_stat.st_uid != 0 or runtime_stat.st_mode & 0o077:
        raise GuardError("unsafe runtime directory")
    # Single service instance, plus kernel package locks for concurrent tools.
    # No stale socket deletion: abnormal leftovers require an explicit review.
    locks = PackageLocks()
    guard = None
    listener = None
    stop = []
    def request_stop(_signum, _frame):
        stop.append(True)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        for path in LOCK_PATHS:
            secure_regular(Path(path))
        locks.acquire()
        snapshot = preflight()  # Under ALL locks, closing the preflight race.
        candidate = Guard(lease, locks)
        candidate.status("startup")
        marker = RUNTIME / "engaged"
        with marker.open("x") as handle:
            handle.write(os.environ["INVOCATION_ID"])
        guard = candidate
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(RUNTIME / "guard.sock"))
        os.chmod(RUNTIME / "guard.sock", 0o600)
        listener.listen(4)
        listener.settimeout(0.5)
        audit("protected", tid=lease["tid"], expires_at_ms=guard.deadline.expires,
              preflight=snapshot, manifest_sha256=hashlib.sha256(CONFIG.read_bytes()).hexdigest())
        notify("READY=1\nWATCHDOG=1")
        while not stop:
            guard.status("tick")
            notify("WATCHDOG=1")
            try:
                peer, _ = listener.accept()
            except socket.timeout:
                continue
            with peer:
                peer.settimeout(1)
                try:
                    data = peer.recv(1025)
                    reply = status_request(data, guard)
                    peer.sendall(json.dumps(reply).encode())
                except (ValueError, OSError, GuardError):
                    # A disconnected read-only client must not terminate the
                    # lease or its package locks. No mutation RPC is exposed.
                    continue
    except GuardError as exc:
        audit("refused_or_expired", reason=str(exc))
        raise
    finally:
        if listener is not None:
            listener.close()
        if guard is not None:
            try:
                guard.finish()
                audit("released", gateway_closed=True, update_policy_unchanged=True)
            except Exception:
                audit("release_failed", gateway_closed=False, update_policy_unchanged=True)
                raise
        else:
            locks.release()


def query(nonce):
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise GuardError("invalid status nonce")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(RUNTIME / "guard.sock"))
        client.sendall(json.dumps({"command": "status", "nonce": nonce}).encode())
        chunks = bytearray()
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > 8192:
                raise GuardError("oversized response")
    response = json.loads(chunks)
    if response.get("nonce") != nonce:
        raise GuardError("stale status response")
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "status", "close-gateway"))
    parser.add_argument("--nonce", default="")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise GuardError("host guard requires Linux")
    if args.action == "run":
        serve()
    elif args.action == "status":
        print(json.dumps(query(args.nonce)))
    else:
        # Independent systemd fallback for SIGKILL/uncaught daemon failure.
        # Failed admission must never stop a gateway it did not protect.
        marker = RUNTIME / "engaged"
        if marker.exists() and marker.read_text() == os.environ.get("INVOCATION_ID"):
            close_gateway()
            audit("systemd_close_confirmed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        audit("failed", error_type=type(error).__name__)
        sys.exit(1)
