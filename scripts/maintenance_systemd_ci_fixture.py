#!/usr/bin/env python3
"""CI-only path/unit adapter. Never install this adapter on a contest host.

The guard, preflight commands, supervisor queries, notifications, deadlines and
shutdown implementation are unchanged. Only files and unit names are remapped.
Gateway/Docker/update units are disposable fixtures, NOT real product services.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


def busy_locks(paths):
    result = []
    for path in paths:
        with open(path, "r+") as handle:
            try:
                fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result.append(False)
            except BlockingIOError:
                result.append(True)
    return result


def main():
    config_path, action = sys.argv[1:]
    config = json.loads(Path(config_path).read_text())
    if not str(Path(config_path).resolve()).startswith("/root/noi-systemd-ci-"):
        raise RuntimeError("not a disposable CI fixture")
    if action == "gateway-stopped":
        with open(config["closure"], "x") as handle:
            json.dump({"monotonic": time.monotonic(), "locks_busy": busy_locks(config["locks"])}, handle)
        return
    spec = importlib.util.spec_from_file_location("guard", config["guard_source"])
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    host.CONFIG = Path(config["lease"])
    host.RUNTIME = Path(config["runtime"])
    host.LOCK_PATHS = tuple(config["locks"])
    original_locks, original_command = host.PackageLocks, host.command
    host.PackageLocks = lambda: original_locks(host.LOCK_PATHS)

    def mapped_command(*args, **kwargs):
        # Query the REAL systemd properties, never synthesize state or READY.
        return original_command(*(config["units"].get(arg, arg) for arg in args), **kwargs)

    host.command = mapped_command
    sys.argv = [sys.argv[0], action]
    try:
        host.main()
    except Exception as error:
        host.audit("failed", error_type=type(error).__name__)
        raise


if __name__ == "__main__":
    main()
