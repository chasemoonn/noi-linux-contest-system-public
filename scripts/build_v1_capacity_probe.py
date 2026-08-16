#!/usr/bin/env python3
"""Freeze site configuration into one auditable capacity probe executable."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import pprint
import stat
import sys
import tempfile

from v1_capacity_measurement_probe import ProbeError, validate_config


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "v1_capacity_measurement_probe.py"
MARKER = "EMBEDDED_CONFIG = None"


class BuildError(RuntimeError):
    pass


def require_root_source(path: Path, label: str) -> Path:
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    if requested != resolved:
        raise BuildError(f"{label} must be canonical")
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        current = current / part
        info = current.lstat()
        if current == resolved:
            safe_type = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        else:
            safe_type = stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
        if not safe_type or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise BuildError(f"{label} or its ancestor is unsafe")
    return resolved


def read_private_json(path: Path) -> dict:
    requested = Path(os.path.abspath(path))
    if path != requested or requested != requested.resolve(strict=True):
        raise BuildError("probe configuration path must be absolute and canonical")
    if platform.system().lower() == "linux":
        current = Path(requested.anchor)
        for part in requested.parts[1:-1]:
            current = current / part
            info = current.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise BuildError("probe configuration has an unsafe ancestor")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(requested, flags)
    except OSError as exc:
        raise BuildError("cannot open probe configuration safely") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size <= 0
            or info.st_size > 1024 * 1024
            or (platform.system().lower() == "linux" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077))
        ):
            raise BuildError("probe configuration metadata is unsafe")
        raw = b""
        while len(raw) <= info.st_size:
            block = os.read(descriptor, min(65536, info.st_size + 1 - len(raw)))
            if not block:
                break
            raw += block
        if len(raw) != info.st_size:
            raise BuildError("probe configuration changed while reading")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildError("probe configuration is not strict UTF-8 JSON") from exc
    return validate_config(value)


def render(config: dict) -> bytes:
    source_path = (
        require_root_source(TEMPLATE, "capacity probe template")
        if platform.system().lower() == "linux"
        else TEMPLATE
    )
    source = source_path.read_text(encoding="utf-8")
    if source.count(MARKER) != 1:
        raise BuildError("capacity probe template marker differs")
    embedded = "EMBEDDED_CONFIG = " + pprint.pformat(config, sort_dicts=True, width=100)
    rendered = source.replace(MARKER, embedded)
    compile(rendered, "<v1-capacity-measurement-probe>", "exec")
    return rendered.encode("utf-8")


def publish(path: Path, raw: bytes) -> str:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        raise BuildError("probe output already exists")
    parent = requested.parent.resolve(strict=True)
    if parent != requested.parent:
        raise BuildError("probe output parent must be canonical")
    if platform.system().lower() == "linux":
        current = Path(parent.anchor)
        for part in parent.parts[1:]:
            current = current / part
            info = current.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise BuildError("probe output parent has an unsafe ancestor")
        if stat.S_IMODE(parent.stat().st_mode) & 0o077:
            raise BuildError("probe output parent must be root-owned mode 0700")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".v1-capacity-probe-", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o500)
        try:
            os.link(temporary, requested, follow_symlinks=False)
        except FileExistsError as exc:
            raise BuildError("probe output appeared concurrently") from exc
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if platform.system().lower() == "linux":
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise BuildError("capacity probe build requires Linux root")
        digest = publish(args.output, render(read_private_json(args.config)))
        print(json.dumps({"probe_sha256": digest, "status": "built"}, sort_keys=True))
        return 0
    except (BuildError, ProbeError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
