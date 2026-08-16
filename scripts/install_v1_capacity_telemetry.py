#!/usr/bin/env python3
"""Install one signed browser telemetry envelope from stdin without rollback."""

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


NAMESPACE = "noi-v1-capacity-telemetry"
MARKER = re.compile(r"NOI-V1-QUAL-[A-Z0-9]{16,64}")
SIGNER = re.compile(r"[A-Za-z0-9_.@+-]{1,80}")
SSH_PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/=]{40,160}(?: [^\r\n]{1,120})?")


class InstallError(RuntimeError):
    pass


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def exact(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise InstallError(f"{label} field set differs")
    return value


def require_root_directory(path: Path) -> Path:
    requested = Path(os.path.abspath(path))
    if requested != path or not requested.is_absolute():
        raise InstallError("output directory must be canonical and absolute")
    current = Path(requested.anchor)
    for part in requested.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise InstallError("output directory is unavailable") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise InstallError("output directory metadata is unsafe")
    return requested


def require_root_binary(path: Path) -> Path:
    requested = Path(os.path.abspath(path))
    try:
        resolved = requested.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise InstallError("ssh-keygen is unavailable") from exc
    if (
        requested != resolved
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise InstallError("ssh-keygen metadata is unsafe")
    current = Path(resolved.anchor)
    for part in resolved.parts[1:-1]:
        current /= part
        ancestor = current.lstat()
        if (
            not stat.S_ISDIR(ancestor.st_mode)
            or stat.S_ISLNK(ancestor.st_mode)
            or ancestor.st_uid != 0
            or stat.S_IMODE(ancestor.st_mode) & 0o022
        ):
            raise InstallError("ssh-keygen ancestor is unsafe")
    return resolved


def existing_sequence(path: Path) -> int:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise InstallError("existing telemetry metadata is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return int(value["payload"]["sequence"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InstallError("existing telemetry is invalid") from exc


def verify(raw: bytes, signer: str, public_key: str, ssh_keygen: Path) -> tuple[dict, bytes]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("telemetry envelope is not strict UTF-8 JSON") from exc
    envelope = exact(
        value, {"schema_version", "namespace", "signer", "payload", "signature_base64"},
        "telemetry envelope"
    )
    if raw != canonical_json(envelope) or envelope["schema_version"] != 1 or \
            envelope["namespace"] != NAMESPACE or envelope["signer"] != signer:
        raise InstallError("telemetry envelope identity differs")
    payload = exact(
        envelope["payload"], {
            "schema_version", "transport_profile", "qualification_marker", "seat_set_sha256", "formal_seat_count", "sequence",
            "window_started_at", "observed_at", "rtt_samples_ms", "packet_loss_percent",
            "websocket_reconnects", "key_to_frame_samples_ms",
        }, "telemetry payload"
    )
    if payload["schema_version"] != 1 or payload["transport_profile"] != "direct_http" or \
            not isinstance(payload["qualification_marker"], str) or \
            not MARKER.fullmatch(payload["qualification_marker"]) or \
            not isinstance(payload["seat_set_sha256"], str) or \
            not re.fullmatch(r"[a-f0-9]{64}", payload["seat_set_sha256"]) or \
            isinstance(payload["sequence"], bool) or not isinstance(payload["sequence"], int) or \
            payload["sequence"] < 1 or payload["formal_seat_count"] != 15:
        raise InstallError("telemetry payload identity is invalid")
    try:
        signature = base64.b64decode(envelope["signature_base64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise InstallError("telemetry signature is invalid") from exc
    if not 32 <= len(signature) <= 131072:
        raise InstallError("telemetry signature size is invalid")
    with tempfile.TemporaryDirectory(prefix="noi-v1-install-") as temporary:
        root = Path(temporary)
        allowed = root / "allowed_signers"
        signature_path = root / "telemetry.sig"
        allowed.write_text(f"{signer} {public_key}\n", encoding="utf-8")
        signature_path.write_bytes(signature)
        result = subprocess.run(
            [str(ssh_keygen), "-Y", "verify", "-f", str(allowed), "-I", signer,
             "-n", NAMESPACE, "-s", str(signature_path)],
            input=canonical_json(payload), capture_output=True, check=False, timeout=10,
        )
    if result.returncode != 0:
        raise InstallError("telemetry signature is invalid")
    return payload, canonical_json(envelope)


def install(raw: bytes, output: Path, signer: str, public_key: str, ssh_keygen: Path) -> int:
    if not SIGNER.fullmatch(signer) or not SSH_PUBLIC_KEY.fullmatch(public_key):
        raise InstallError("signer configuration is invalid")
    parent = require_root_directory(output.parent)
    ssh_keygen = require_root_binary(ssh_keygen)
    payload, normalized = verify(raw, signer, public_key, ssh_keygen)
    prior = existing_sequence(output)
    if payload["sequence"] <= prior:
        raise InstallError("telemetry sequence did not advance")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".telemetry-", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(normalized)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise InstallError("telemetry envelope write did not complete")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, output)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return payload["sequence"]


def main() -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise InstallError("telemetry installer must run as root on Linux")
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
            raise InstallError("telemetry envelope size is invalid")
        sequence = install(raw, args.output, args.signer, args.public_key, args.ssh_keygen)
        print(f"TELEMETRY_INSTALLED sequence={sequence}")
        return 0
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallError as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        raise SystemExit(2)
