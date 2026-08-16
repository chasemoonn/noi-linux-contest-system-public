#!/usr/bin/env python3
"""Verify and atomically install one ordinary-OJ signed telemetry envelope."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


NAMESPACE = "noi-v1-capacity-ordinary-oj"
SIGNER = re.compile(r"[A-Za-z0-9_.@+-]{1,80}")
SSH_PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?")
HEX64 = re.compile(r"[a-f0-9]{64}")


class InstallError(RuntimeError):
    pass


def exact(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise InstallError(f"{label} field set differs")
    return value


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def root_path(path: Path, *, directory: bool = False, executable: bool = False) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    if path != requested or requested != resolved:
        raise InstallError("installer path must be canonical")
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        current /= part; info = current.lstat()
        leaf = current == resolved
        valid_type = stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) \
            if (not leaf or directory) else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        permission_mask = 0o077 if leaf and directory else 0o022
        if not valid_type or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & permission_mask:
            raise InstallError("installer path metadata is unsafe")
    if executable and not os.access(resolved, os.X_OK):
        raise InstallError("installer executable is not executable")
    return resolved


def prior_sequence(path: Path) -> int:
    if not os.path.lexists(path):
        return 0
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or \
            (sys.platform == "linux" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077)):
        raise InstallError("existing ordinary OJ telemetry metadata is unsafe")
    try:
        value = json.loads(path.read_text())
        return int(value["payload"]["sequence"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InstallError("existing ordinary OJ telemetry is invalid") from exc


def verify(raw: bytes, signer: str, public_key: str, ssh_keygen: Path) -> tuple[dict, bytes]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("ordinary OJ envelope is not strict UTF-8 JSON") from exc
    envelope = exact(value, {"schema_version", "namespace", "signer", "payload", "signature_base64"},
                     "ordinary OJ envelope")
    if raw != canonical(envelope) or envelope["schema_version"] != 1 or \
            envelope["namespace"] != NAMESPACE or envelope["signer"] != signer:
        raise InstallError("ordinary OJ envelope identity differs")
    payload = exact(envelope["payload"], {
        "schema_version", "qualification_marker", "sequence", "observed_at", "homepage_status", "login_status",
        "prep_health_ok", "prep_database_ok", "ordinary_oj_errors", "ordinary_oj_restarts",
        "ordinary_oj_pid_changes", "credential_leaks", "result_leaks", "pm2_fingerprint_sha256",
    }, "ordinary OJ payload")
    if payload["schema_version"] != 1 or isinstance(payload["sequence"], bool) or \
            not isinstance(payload["sequence"], int) or payload["sequence"] < 1 or \
            not isinstance(payload["qualification_marker"], str) or \
            not re.fullmatch(r"NOI-V1-QUAL-[A-Z0-9]{16,64}", payload["qualification_marker"]) or \
            payload["homepage_status"] != 200 or payload["login_status"] != 200 or \
            payload["prep_health_ok"] is not True or payload["prep_database_ok"] is not True:
        raise InstallError("ordinary OJ payload state differs")
    for key in ("ordinary_oj_errors", "ordinary_oj_restarts", "ordinary_oj_pid_changes",
                "credential_leaks", "result_leaks"):
        if payload[key] != 0:
            raise InstallError("ordinary OJ payload contains a non-zero failure")
    if not isinstance(payload["pm2_fingerprint_sha256"], str) or not HEX64.fullmatch(
            payload["pm2_fingerprint_sha256"]):
        raise InstallError("ordinary OJ PM2 fingerprint is invalid")
    try:
        signature = base64.b64decode(envelope["signature_base64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise InstallError("ordinary OJ signature is invalid") from exc
    if not 32 <= len(signature) <= 131072:
        raise InstallError("ordinary OJ signature size is invalid")
    with tempfile.TemporaryDirectory(prefix="noi-v1-ordinary-install-") as temporary:
        allowed = Path(temporary) / "allowed"; signature_path = Path(temporary) / "signature"
        allowed.write_text(f"{signer} {public_key}\n"); signature_path.write_bytes(signature)
        result = subprocess.run(
            [str(ssh_keygen), "-Y", "verify", "-f", str(allowed), "-I", signer,
             "-n", NAMESPACE, "-s", str(signature_path)], input=canonical(payload),
            capture_output=True, check=False, timeout=10,
        )
    if result.returncode:
        raise InstallError("ordinary OJ signature is invalid")
    return payload, canonical(envelope)


def install(raw: bytes, output: Path, signer: str, public_key: str, ssh_keygen: Path) -> int:
    if not SIGNER.fullmatch(signer) or not SSH_PUBLIC_KEY.fullmatch(public_key):
        raise InstallError("ordinary OJ installer signer configuration is invalid")
    parent = root_path(output.parent, directory=True)
    binary = root_path(ssh_keygen, executable=True)
    payload, normalized = verify(raw, signer, public_key, binary)
    if payload["sequence"] <= prior_sequence(output):
        raise InstallError("ordinary OJ telemetry sequence did not advance")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ordinary-oj-", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(normalized); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, output)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass
    return payload["sequence"]


def main() -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise InstallError("ordinary OJ telemetry installer requires Linux root")
    previous_umask = os.umask(0o077)
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--output", required=True, type=Path)
        parser.add_argument("--signer", required=True)
        parser.add_argument("--public-key", required=True)
        parser.add_argument("--ssh-keygen", default="/usr/bin/ssh-keygen", type=Path)
        args = parser.parse_args()
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if not raw or len(raw) > 1024 * 1024:
            raise InstallError("ordinary OJ envelope size is invalid")
        sequence = install(raw, args.output, args.signer, args.public_key, args.ssh_keygen)
        print(f"TELEMETRY_INSTALLED sequence={sequence}")
        return 0
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    try: raise SystemExit(main())
    except InstallError as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); raise SystemExit(2)
