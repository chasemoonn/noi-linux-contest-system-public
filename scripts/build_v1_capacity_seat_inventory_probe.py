#!/usr/bin/env python3
"""Freeze a root-only 15+2 seat inventory into one executable probe."""

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

from v1_capacity_seat_inventory_probe import validate_config


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "v1_capacity_seat_inventory_probe.py"
MARKER = "EMBEDDED_CONFIG = None"


class BuildError(RuntimeError):
    pass


def require_safe_ancestors(path: Path, *, include_leaf: bool) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if include_leaf else path.parts[1:-1]
    for part in parts:
        current /= part
        info = current.lstat()
        if current == path and include_leaf:
            safe_type = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        else:
            safe_type = stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
        if not safe_type or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise BuildError("seat probe path metadata is unsafe")


def read_config(path: Path) -> dict:
    requested = Path(os.path.abspath(path))
    if requested != path or requested != requested.resolve(strict=True):
        raise BuildError("seat probe config must be canonical and absolute")
    require_safe_ancestors(requested, include_leaf=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(requested, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or \
                stat.S_IMODE(info.st_mode) & 0o077 or not 0 < info.st_size <= 1024 * 1024:
            raise BuildError("seat probe config metadata is unsafe")
        raw = os.read(descriptor, info.st_size + 1)
        if len(raw) != info.st_size:
            raise BuildError("seat probe config changed while reading")
    finally:
        os.close(descriptor)
    try:
        return validate_config(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BuildError("seat probe config is invalid") from exc


def render(config: dict) -> bytes:
    if platform.system().lower() == "linux":
        require_safe_ancestors(TEMPLATE.resolve(strict=True), include_leaf=True)
    source = TEMPLATE.read_text(encoding="utf-8")
    if source.count(MARKER) != 1:
        raise BuildError("seat probe template marker differs")
    rendered = source.replace(MARKER, "EMBEDDED_CONFIG = " + pprint.pformat(config, sort_dicts=True))
    compile(rendered, "<v1-capacity-seat-inventory-probe>", "exec")
    return rendered.encode()


def publish(path: Path, raw: bytes) -> str:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        raise BuildError("seat probe output already exists")
    parent = requested.parent.resolve(strict=True)
    info = parent.stat()
    if parent != requested.parent or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise BuildError("seat probe output parent is unsafe")
    require_safe_ancestors(parent, include_leaf=False)
    descriptor, name = tempfile.mkstemp(prefix=".seat-probe-", dir=parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o500)
        os.link(temporary, requested, follow_symlinks=False)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise BuildError("seat probe build requires Linux root")
        digest = publish(args.output, render(read_config(args.config)))
        print(json.dumps({"probe_sha256": digest, "status": "built"}, sort_keys=True))
        return 0
    except (BuildError, OSError, ValueError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
