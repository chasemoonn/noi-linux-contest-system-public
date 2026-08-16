#!/usr/bin/env python3
"""Run the complete Linux source gate and emit qualification evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
HEX40 = re.compile(r"^[a-f0-9]{40}$")


class GateError(RuntimeError):
    pass


def git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise GateError(f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def command_plan() -> list[tuple[str, list[str], Path]]:
    node_tests = sorted(
        str(path.relative_to(ROOT / "hydro-plugin-orchestrator"))
        for path in (ROOT / "hydro-plugin-orchestrator/tests").glob("*.test.js")
    )
    shell_scripts = sorted(
        str(path.relative_to(ROOT))
        for directory in (ROOT / "deploy", ROOT / "scripts")
        for path in directory.glob("*.sh")
    )
    if not node_tests or not shell_scripts:
        raise GateError("tracked Node tests or shell scripts are missing")
    return [
        (
            "python_compile",
            [sys.executable, "-m", "compileall", "-q", "."],
            ROOT / "orchestrator",
        ),
        (
            "python_unit_tests",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            ROOT / "orchestrator",
        ),
        (
            "hydro_plugin_syntax",
            ["node", "--check", "index.js"],
            ROOT / "hydro-plugin-orchestrator",
        ),
        (
            "hydro_plugin_tests",
            ["node", "--test", *node_tests],
            ROOT / "hydro-plugin-orchestrator",
        ),
        (
            "submission_fault_injection",
            [sys.executable, "scripts/run_v1_fault_injection.py", "--require-linux"],
            ROOT,
        ),
        ("deployment_shell_syntax", ["bash", "-n", *shell_scripts], ROOT),
        ("demo_reproducibility", [sys.executable, "scripts/build_demo.py", "--check"], ROOT),
        ("v1_product_contract", [sys.executable, "scripts/check_v1_product_contract.py"], ROOT),
        (
            "qualification_schema",
            [
                sys.executable,
                "scripts/verify_v1_qualification.py",
                "release/v1-qualification-report.example.json",
            ],
            ROOT,
        ),
        ("public_release_boundary", [sys.executable, "scripts/check_public_release.py"], ROOT),
    ]


def atomic_bytes(path: Path, raw: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def run_gate(
    index: int,
    name: str,
    command: list[str],
    cwd: Path,
    log_directory: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            env=environment,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(f"{name} could not complete: {exc}") from exc
    duration_ms = round((time.monotonic() - started) * 1000)
    sys.stdout.buffer.write(result.stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(result.stderr)
    sys.stderr.buffer.flush()
    if result.returncode:
        raise GateError(f"{name} failed with exit {result.returncode}")
    stdout_name = f"{index:02d}-{name}.stdout.log"
    stderr_name = f"{index:02d}-{name}.stderr.log"
    atomic_bytes(log_directory / stdout_name, result.stdout)
    atomic_bytes(log_directory / stderr_name, result.stderr)
    return {
        "duration_ms": duration_ms,
        "name": name,
        "status": "passed",
        "stderr_file": stderr_name,
        "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
        "stdout_file": stdout_name,
        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
    }


def private_linux_temp_root() -> Path:
    import pwd

    root_home = Path(pwd.getpwuid(0).pw_dir)
    try:
        requested = Path(os.path.abspath(root_home))
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GateError("Linux root home cannot be resolved safely") from exc
    if requested != resolved:
        raise GateError("Linux root home must not contain symbolic links")
    for ancestor in (resolved, *resolved.parents):
        try:
            info = os.lstat(ancestor)
        except OSError as exc:
            raise GateError("Linux root home ancestor cannot be inspected") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
        ):
            raise GateError("Linux root home ancestor is unsafe")
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".noi-v1-linux-ci-", dir=resolved))
        os.chmod(temporary, 0o700)
        info = os.lstat(temporary)
    except OSError as exc:
        raise GateError("private Linux CI temporary root cannot be created") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise GateError("private Linux CI temporary root metadata differs")
    return temporary


def gate_environment(temporary: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("TMPDIR", "TMP", "TEMP"):
        environment[name] = str(temporary)
    return environment


def runtime_version(command: list[str], label: str) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(f"cannot read {label} version: {exc}") from exc
    value = (result.stdout or result.stderr).strip()
    if result.returncode or not value or len(value) > 200 or any(ord(ch) < 32 for ch in value):
        raise GateError(f"invalid {label} version")
    return value


def atomic_json(path: Path, document: dict[str, object]) -> str:
    parent = path.parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise GateError("evidence output must be absent or a regular file")
    raw = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".v1-linux-ci-", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(raw).hexdigest()


def describe() -> dict[str, object]:
    return {
        "evidence_schema": "v1-linux-ci-evidence.schema.json",
        "gates": [name for name, _command, _cwd in command_plan()],
        "required_platform": "linux",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--log-directory", type=Path)
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args()
    try:
        if args.describe:
            if args.output is not None or args.log_directory is not None:
                raise GateError("--describe cannot be combined with output arguments")
            print(json.dumps(describe(), sort_keys=True))
            return 0
        if args.output is None or args.log_directory is None:
            raise GateError("--output and --log-directory are required")
        if platform.system().lower() != "linux":
            raise GateError("the complete qualification gate requires Linux")
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise GateError("the complete qualification gate requires Linux root")
        if git("status", "--porcelain=v1", "--untracked-files=all"):
            raise GateError("Git worktree must be clean before Linux CI")
        revision = git("rev-parse", "HEAD")
        tree = git("rev-parse", "HEAD^{tree}")
        if not HEX40.fullmatch(revision) or not HEX40.fullmatch(tree):
            raise GateError("Git revision or tree is invalid")
        log_directory = args.log_directory.resolve()
        if log_directory.exists():
            raise GateError("log directory must not already exist")
        log_directory.mkdir(parents=True, mode=0o700)
        started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        temporary = private_linux_temp_root()
        try:
            environment = gate_environment(temporary)
            gates = [
                run_gate(index, *row, log_directory, environment)
                for index, row in enumerate(command_plan(), 1)
            ]
        finally:
            try:
                shutil.rmtree(temporary)
            except OSError as exc:
                raise GateError("private Linux CI temporary root cleanup failed") from exc
        finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if git("status", "--porcelain=v1", "--untracked-files=no"):
            raise GateError("Linux CI changed tracked source files")
        document = {
            "$schema": "v1-linux-ci-evidence.schema.json",
            "schema_version": 1,
            "status": "passed",
            "source": {"revision": revision, "tree": tree},
            "environment": {
                "architecture": platform.machine(),
                "effective_uid": os.geteuid(),
                "kernel": platform.release(),
                "node": runtime_version(["node", "--version"], "Node.js"),
                "python": platform.python_version(),
                "system": "linux",
            },
            "finished_at": finished_at,
            "gates": gates,
            "started_at": started_at,
        }
        digest = atomic_json(args.output.resolve(), document)
        print(
            json.dumps(
                {
                    "evidence": str(args.output),
                    "evidence_sha256": digest,
                    "revision": revision,
                    "status": "passed",
                },
                sort_keys=True,
            )
        )
        return 0
    except GateError as exc:
        print(f"LINUX_CI_FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
