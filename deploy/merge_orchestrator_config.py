#!/usr/bin/env python3
"""Atomically update the one orchestrator YAML key owned by installers.

The deployment scripts must not deserialize and rewrite the full YAML document:
doing so could discard comments or future keys.  This helper changes only
``hydro.notify_allowed_https_hosts`` and preserves every other byte.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import tempfile


HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:")
MANAGED_KEY_RE = re.compile(r"^  notify_allowed_https_hosts\s*:")


def merge_notify_allowed_https_host(source: str, host: str) -> str:
    """Return *source* with only the managed Hydro allowlist replaced."""
    if not HOST_RE.fullmatch(host):
        raise ValueError("notify allowlist host must be one exact DNS name")
    newline = "\r\n" if "\r\n" in source else "\n"
    lines = source.splitlines(keepends=True)
    hydro_indexes = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"hydro:\s*(?:#.*)?(?:\r?\n)?", line)
    ]
    if len(hydro_indexes) != 1:
        raise ValueError("config.yaml must contain exactly one top-level hydro block")
    hydro_start = hydro_indexes[0]
    hydro_end = len(lines)
    for index in range(hydro_start + 1, len(lines)):
        line = lines[index]
        if line.startswith((" ", "\t", "#", "\r", "\n")):
            continue
        if TOP_LEVEL_KEY_RE.match(line):
            hydro_end = index
            break
        raise ValueError("cannot safely determine the end of the hydro block")

    managed = [
        index
        for index in range(hydro_start + 1, hydro_end)
        if MANAGED_KEY_RE.match(lines[index])
    ]
    if len(managed) > 1:
        raise ValueError("duplicate hydro.notify_allowed_https_hosts keys")
    replacement = [
        f"  notify_allowed_https_hosts:{newline}",
        f'    - "{host}"{newline}',
    ]
    if not managed:
        if hydro_end and lines[hydro_end - 1] and not lines[hydro_end - 1].endswith(
            ("\n", "\r")
        ):
            lines[hydro_end - 1] += newline
        lines[hydro_end:hydro_end] = replacement
        return "".join(lines)

    value_start = managed[0]
    value_end = value_start + 1
    while value_end < hydro_end:
        line = lines[value_end]
        if line.strip() and line.startswith(("    ", "\t")):
            value_end += 1
            continue
        break
    lines[value_start:value_end] = replacement
    return "".join(lines)


def merge_config(path: Path, host: str) -> bool:
    """Atomically merge the allowlist and return whether bytes changed."""
    source = path.read_bytes().decode("utf-8")
    merged = merge_notify_allowed_https_host(source, host)
    if merged == source:
        return False
    mode = path.stat().st_mode & 0o777
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(merged)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("host")
    args = parser.parse_args()
    changed = merge_config(args.config, args.host)
    print(f"orchestrator_config_allowlist={'updated' if changed else 'unchanged'}")


if __name__ == "__main__":
    main()
