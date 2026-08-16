#!/usr/bin/env python3
"""Freeze one site's ordinary-OJ qualification observer into an executable."""
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

from v1_capacity_ordinary_oj_agent import AgentError, validate_config


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "v1_capacity_ordinary_oj_agent.py"
MARKER = "EMBEDDED_CONFIG = None"


class BuildError(RuntimeError):
    pass


def safe_root_path(path: Path, *, leaf_file: bool) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    if requested != resolved:
        raise BuildError("ordinary OJ agent build path must be canonical")
    if platform.system().lower() == "linux":
        current = Path(resolved.anchor)
        for part in resolved.parts[1:]:
            current /= part; info = current.lstat(); leaf = current == resolved
            valid_type = stat.S_ISREG(info.st_mode) and info.st_nlink == 1 \
                if leaf and leaf_file else stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            if not valid_type or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise BuildError("ordinary OJ agent build path metadata is unsafe")
    return resolved


def read_private_json(path: Path) -> dict:
    requested = Path(os.path.abspath(path))
    if requested != path or requested != requested.resolve(strict=True):
        raise BuildError("ordinary OJ agent configuration must be absolute and canonical")
    safe_root_path(requested, leaf_file=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(requested, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0 or \
                info.st_size > 1024 * 1024 or (platform.system().lower() == "linux" and
                (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077)):
            raise BuildError("ordinary OJ agent configuration metadata is unsafe")
        raw = os.read(descriptor, info.st_size + 1)
        if len(raw) != info.st_size:
            raise BuildError("ordinary OJ agent configuration changed while reading")
    finally:
        os.close(descriptor)
    try:
        return validate_config(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildError("ordinary OJ agent configuration is not strict JSON") from exc


def render(config: dict) -> bytes:
    source_path = safe_root_path(TEMPLATE, leaf_file=True) \
        if platform.system().lower() == "linux" else TEMPLATE
    source = source_path.read_text(encoding="utf-8")
    if source.count(MARKER) != 1:
        raise BuildError("ordinary OJ agent template marker differs")
    rendered = source.replace(
        MARKER, "EMBEDDED_CONFIG = " + pprint.pformat(config, sort_dicts=True, width=100)
    )
    compile(rendered, "<v1-capacity-ordinary-oj-agent>", "exec")
    return rendered.encode()


def publish(path: Path, raw: bytes) -> str:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        raise BuildError("ordinary OJ agent output already exists")
    parent = requested.parent.resolve(strict=True)
    if parent != requested.parent or (platform.system().lower() == "linux" and
            (parent.stat().st_uid != 0 or stat.S_IMODE(parent.stat().st_mode) & 0o077)):
        raise BuildError("ordinary OJ agent output parent is unsafe")
    if platform.system().lower() == "linux":
        safe_root_path(parent, leaf_file=False)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".v1-ordinary-agent-", dir=parent)
    temporary = Path(temporary_name)
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
            raise BuildError("ordinary OJ capacity agent build requires Linux root")
        digest = publish(args.output, render(read_private_json(args.config)))
        print(json.dumps({"agent_sha256": digest, "status": "built"}, sort_keys=True))
        return 0
    except (BuildError, AgentError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
