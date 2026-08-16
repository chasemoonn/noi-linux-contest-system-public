#!/usr/bin/env python3
"""Freeze one controller-only network fault agent configuration."""
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

from v1_capacity_network_fault_agent import AgentError, validate_config


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "v1_capacity_network_fault_agent.py"
MARKER = "EMBEDDED_CONFIG = None"


class BuildError(RuntimeError): pass


def safe_path(path: Path, *, file: bool) -> Path:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    if requested != path or requested != resolved:
        raise BuildError("network fault agent path must be canonical and absolute")
    if platform.system().lower() == "linux":
        current = Path(resolved.anchor)
        for part in resolved.parts[1:]:
            current /= part; info = current.lstat(); leaf = current == resolved
            good = stat.S_ISREG(info.st_mode) and info.st_nlink == 1 if leaf and file \
                else stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            if not good or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise BuildError("network fault agent path metadata is unsafe")
    return resolved


def read_config(path: Path) -> dict:
    path = safe_path(path, file=True)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not 0 < info.st_size <= 1024 * 1024 or (platform.system().lower() == "linux" and
                (info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077)):
            raise BuildError("network fault agent config metadata is unsafe")
        raw = os.read(descriptor, info.st_size + 1)
        if len(raw) != info.st_size:
            raise BuildError("network fault agent config changed while reading")
    finally: os.close(descriptor)
    try: return validate_config(json.loads(raw.decode()))
    except (UnicodeDecodeError, json.JSONDecodeError, AgentError) as exc:
        raise BuildError("network fault agent config is invalid") from exc


def render(config: dict) -> bytes:
    if platform.system().lower() == "linux": safe_path(TEMPLATE, file=True)
    source = TEMPLATE.read_text(encoding="utf-8")
    if source.count(MARKER) != 1: raise BuildError("network fault agent template marker differs")
    rendered = source.replace(MARKER, "EMBEDDED_CONFIG = " + pprint.pformat(config, sort_dicts=True))
    compile(rendered, "<v1-capacity-network-fault-agent>", "exec")
    return rendered.encode()


def publish(path: Path, raw: bytes) -> str:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested): raise BuildError("network fault agent output already exists")
    parent = safe_path(requested.parent, file=False)
    if platform.system().lower() == "linux" and stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise BuildError("network fault agent output parent is unsafe")
    descriptor, name = tempfile.mkstemp(prefix=".network-fault-agent-", dir=parent); temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o500); os.link(temporary, requested, follow_symlinks=False)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path); args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise BuildError("network fault agent build requires Linux root")
        digest = publish(args.output, render(read_config(args.config)))
        print(json.dumps({"agent_sha256": digest, "status": "built"}, sort_keys=True)); return 0
    except (BuildError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
