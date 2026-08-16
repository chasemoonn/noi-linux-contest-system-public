#!/usr/bin/env python3
"""Publish ordinary-OJ telemetry through one pinned forced-command SSH channel."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


TARGET = re.compile(r"[A-Za-z0-9_.-]{1,64}@[A-Za-z0-9.-]{1,253}")


class PublishError(RuntimeError):
    pass


def root_file(path: Path, label: str, *, executable: bool = False) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    if path != requested or requested != resolved:
        raise PublishError(f"{label} must be canonical")
    current = Path(resolved.anchor)
    for part in resolved.parts[1:-1]:
        current /= part; ancestor = current.lstat()
        if not stat.S_ISDIR(ancestor.st_mode) or stat.S_ISLNK(ancestor.st_mode) or \
                (sys.platform == "linux" and (ancestor.st_uid != 0 or
                 stat.S_IMODE(ancestor.st_mode) & 0o022)):
            raise PublishError(f"{label} ancestor metadata is unsafe")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or \
            (sys.platform == "linux" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) &
             (0o022 if executable else 0o077))) or (executable and not os.access(resolved, os.X_OK)):
        raise PublishError(f"{label} metadata is unsafe")
    return resolved


def publish(envelope: Path, ssh: Path, identity: Path, known_hosts: Path, target: str) -> int:
    if not TARGET.fullmatch(target):
        raise PublishError("ordinary OJ publisher target is invalid")
    envelope = root_file(envelope, "ordinary OJ envelope")
    ssh = root_file(ssh, "SSH executable", executable=True)
    identity = root_file(identity, "SSH identity")
    known_hosts = root_file(known_hosts, "SSH known_hosts")
    descriptor = os.open(envelope, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        result = subprocess.run([
            str(ssh), "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-i", str(identity), "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no", "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no", "-o", "ClearAllForwardings=yes",
            "-o", "PermitLocalCommand=no", "-o", "RequestTTY=no", target,
        ], stdin=descriptor, capture_output=True, check=False, timeout=15, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishError("ordinary OJ telemetry publication could not complete") from exc
    finally:
        os.close(descriptor)
    if result.returncode or result.stderr or not re.fullmatch(
            r"TELEMETRY_INSTALLED sequence=[1-9][0-9]*\r?\n", result.stdout):
        raise PublishError("ordinary OJ telemetry publication failed")
    return int(result.stdout.split("=")[1])


def main() -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise PublishError("ordinary OJ telemetry publisher requires Linux root")
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--ssh", default="/usr/bin/ssh", type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    parser.add_argument("--known-hosts", required=True, type=Path)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    sequence = publish(args.envelope, args.ssh, args.identity, args.known_hosts, args.target)
    print(f"TELEMETRY_PUBLISHED sequence={sequence}")
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except PublishError as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); raise SystemExit(2)
