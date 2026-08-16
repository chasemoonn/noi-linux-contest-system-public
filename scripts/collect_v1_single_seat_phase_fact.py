#!/usr/bin/env python3
"""Create one immutable phase fact inside a root-only single-seat session."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile

from verify_v1_single_seat_evidence import (
    EvidenceError,
    PHASES,
    ROLES,
    validate_components,
    validate_context,
    validate_fact,
    validate_source,
)
from collect_v1_components import ComponentError, validate_role_component


ROOT = Path(__file__).resolve().parents[1]
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")


class FactError(RuntimeError):
    pass


def read_regular(path: Path, limit: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FactError(f"cannot open {label} safely: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise FactError(f"{label} must be a bounded single-link regular file")
        raw = b""
        while len(raw) <= limit:
            block = os.read(descriptor, min(65536, limit + 1 - len(raw)))
            if not block:
                break
            raw += block
        if len(raw) != info.st_size:
            raise FactError(f"{label} changed while reading")
        return raw
    finally:
        os.close(descriptor)


def read_json(path: Path, label: str, limit: int = 1024 * 1024) -> tuple[dict, bytes]:
    info = path.lstat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise FactError(f"{label} must be root-owned mode 0600 or stricter")
    raw = read_regular(path, limit, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise FactError(f"{label} must be an object")
    return value, raw


def read_private_artifact(path: Path, label: str) -> bytes:
    info = path.lstat()
    if platform.system().lower() == "linux" and (
        info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise FactError(f"{label} must be root-owned mode 0600 or stricter")
    return read_regular(path, 16 * 1024 * 1024, label)


def validate_session(value: dict) -> dict:
    if set(value) != {
        "$schema",
        "schema_version",
        "session_id",
        "created_at",
        "source",
        "components",
        "component_facts",
        "context",
    }:
        raise FactError("session shape differs")
    if value["$schema"] != "v1-single-seat-session.schema.json" or value["schema_version"] != 1:
        raise FactError("unsupported single-seat session")
    if not isinstance(value["session_id"], str) or not HEX64.fullmatch(value["session_id"]):
        raise FactError("session ID is invalid")
    try:
        validate_source(value["source"])
        validate_components(value["components"])
        validate_context(value["context"])
    except EvidenceError as exc:
        raise FactError(str(exc)) from exc
    if value["components"]["desktop_source_revision"] != value["source"]["revision"]:
        raise FactError("session component revision differs")
    component_facts = value["component_facts"]
    if not isinstance(component_facts, dict) or set(component_facts) != {
        "control",
        "desktop",
        "oj",
    }:
        raise FactError("session component fact references differ")
    for role, row in component_facts.items():
        if not isinstance(row, dict) or row != {
            "reference": f"components/{role}.json",
            "sha256": row.get("sha256"),
        }:
            raise FactError("session component fact reference is invalid")
        if not isinstance(row["sha256"], str) or not HEX64.fullmatch(row["sha256"]):
            raise FactError("session component fact digest is invalid")
    created_at = value["created_at"]
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise FactError("session created_at is invalid")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FactError("session created_at is invalid") from exc
    return value


def verify_frozen_component_facts(session_dir: Path, session: dict) -> None:
    component_dir = session_dir / "components"
    require_private_directory(component_dir, "component fact directory")
    expected_names = {f"{role}.json" for role in ("control", "desktop", "oj")}
    if {path.name for path in component_dir.iterdir()} != expected_names:
        raise FactError("frozen component fact file set differs")
    for role in ("control", "desktop", "oj"):
        row, raw = read_json(component_dir / f"{role}.json", f"frozen {role} component")
        try:
            validate_role_component(row, role)
        except ComponentError as exc:
            raise FactError(str(exc)) from exc
        if hashlib.sha256(raw).hexdigest() != session["component_facts"][role]["sha256"]:
            raise FactError(f"frozen {role} component fact digest differs")
        if not role_matches_session(role, row, session["components"]):
            raise FactError(f"frozen {role} component identity differs")


def git(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments], cwd=ROOT, check=False, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactError("Git identity check could not complete") from exc
    value = result.stdout.strip()
    if result.returncode or (arguments[:1] != ("status",) and not value):
        raise FactError("Git identity check failed")
    return value


def require_private_directory(path: Path, label: str) -> None:
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    if requested != resolved or not resolved.is_dir() or requested.is_symlink():
        raise FactError(f"{label} must be a real canonical directory")
    path = resolved
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise FactError(f"{label} has an unsafe ancestor: {current}")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise FactError(f"{label} must be mode 0700 or stricter")


def parse_artifact(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise FactError("--artifact must use safe-name=/absolute/path")
    name, raw_path = value.split("=", 1)
    if not SAFE_NAME.fullmatch(name):
        raise FactError("artifact name is unsafe")
    path = Path(raw_path)
    if not path.is_absolute():
        raise FactError("artifact source path must be absolute")
    return name, path


def copy_artifact(source: Path, destination: Path) -> str:
    raw = read_private_artifact(source, f"artifact {source.name}")
    return write_artifact(raw, destination)


def write_artifact(raw: bytes, destination: Path) -> str:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return hashlib.sha256(raw).hexdigest()


def create_fact(
    *,
    phase: str,
    role: str,
    session: dict,
    host_id: str,
    observed_at: str,
    ordinary_oj: dict,
    observations: dict,
    artifacts: list[dict],
) -> dict:
    fact = {
        "$schema": "v1-single-seat-phase-fact.schema.json",
        "schema_version": 1,
        "phase": phase,
        "session_id": session["session_id"],
        "observed_at": observed_at,
        "collector": {"anonymous_host_id": host_id, "role": role},
        "source": session["source"],
        "components": session["components"],
        "context": session["context"],
        "ordinary_oj": ordinary_oj,
        "observations": observations,
        "artifacts": artifacts,
    }
    try:
        return validate_fact(fact, phase)
    except EvidenceError as exc:
        raise FactError(str(exc)) from exc


def role_matches_session(role: str, observed: dict, components: dict) -> bool:
    identity = dict(observed)
    identity.pop("observed_at", None)
    expected = {
        "control": {
            "role": "control",
            "orchestrator_image_digest": components["orchestrator_image_digest"],
        },
        "desktop": {
            "role": "desktop",
            "desktop_contract": "finalizer-status-v1",
            "desktop_image_id": components["desktop_image_id"],
            "desktop_source_revision": components["desktop_source_revision"],
        },
        "oj": {
            "role": "oj",
            "hydro_plugin_sha256": components["hydro_plugin_sha256"],
        },
    }[role]
    return identity == expected


def durable_fact(path: Path, value: dict) -> str:
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--role", choices={"control", "desktop", "oj"}, required=True)
    parser.add_argument("--session-directory", type=Path, required=True)
    parser.add_argument("--ordinary-oj", type=Path, required=True)
    parser.add_argument("--observed-components", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--artifact", action="append", default=[])
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise FactError("single-seat fact collection requires Linux root")
        if args.role != ROLES[args.phase]:
            raise FactError(f"phase {args.phase} requires role {ROLES[args.phase]}")
        if git("status", "--porcelain=v1", "--untracked-files=no"):
            raise FactError("tracked Git worktree must be clean")
        session_dir = Path(os.path.abspath(args.session_directory))
        require_private_directory(session_dir, "session directory")
        facts_dir = session_dir / "facts"
        artifacts_root = session_dir / "artifacts"
        require_private_directory(facts_dir, "facts directory")
        require_private_directory(artifacts_root, "artifacts directory")
        pending = [path.name for path in artifacts_root.iterdir() if ".pending-" in path.name]
        if pending:
            raise FactError("session contains an incomplete pending phase")
        session, session_raw = read_json(session_dir / "session.json", "session")
        validate_session(session)
        verify_frozen_component_facts(session_dir, session)
        if git("rev-parse", "HEAD") != session["source"]["revision"] or git("rev-parse", "HEAD^{tree}") != session["source"]["tree"]:
            raise FactError("collector checkout differs from the session")
        ordinary, ordinary_raw = read_json(
            Path(os.path.abspath(args.ordinary_oj)), "ordinary OJ observation"
        )
        observed_components, components_raw = read_json(
            Path(os.path.abspath(args.observed_components)), "observed component identity"
        )
        try:
            validate_role_component(observed_components, args.role)
        except ComponentError as exc:
            raise FactError(str(exc)) from exc
        if not role_matches_session(args.role, observed_components, session["components"]):
            raise FactError("observed components differ from the frozen session")
        phase_time = datetime.now(timezone.utc)
        component_time = datetime.fromisoformat(
            observed_components["observed_at"].replace("Z", "+00:00")
        )
        component_age = (phase_time - component_time).total_seconds()
        if component_age < 0 or component_age > 120:
            raise FactError(
                "component observation must precede the phase fact by at most 120 seconds"
            )
        observations, observations_raw = read_json(
            Path(os.path.abspath(args.observations)), "phase observations"
        )
        payloads = [
            ("ordinary-oj.json", ordinary_raw),
            ("components.json", components_raw),
            ("observations.json", observations_raw),
        ]
        for value in args.artifact:
            name, source = parse_artifact(value)
            payloads.append(
                (name, read_private_artifact(source, f"artifact {source.name}"))
            )
        if len(payloads) > 16:
            raise FactError("phase contains more than 16 artifacts")
        names = [name for name, _ in payloads]
        if len(set(names)) != len(names):
            raise FactError("artifact names contain duplicates")
        rows = []
        for name, raw in payloads:
            digest = hashlib.sha256(raw).hexdigest()
            rows.append({"reference": f"{args.phase}/{name}", "sha256": digest})
        machine = read_regular(Path("/etc/machine-id"), 4096, "machine ID").strip()
        if not machine:
            raise FactError("machine ID is empty")
        host_id = hashlib.sha256(session["session_id"].encode("ascii") + b":" + machine).hexdigest()
        fact = create_fact(
            phase=args.phase,
            role=args.role,
            session=session,
            host_id=host_id,
            observed_at=phase_time.isoformat().replace("+00:00", "Z"),
            ordinary_oj=ordinary,
            observations=observations,
            artifacts=rows,
        )
        phase_dir = artifacts_root / args.phase
        fact_path = facts_dir / f"{args.phase}.json"
        if os.path.lexists(phase_dir) or os.path.lexists(fact_path):
            raise FactError("phase evidence already exists; never overwrite or replay a phase")
        staging_dir = Path(
            tempfile.mkdtemp(prefix=f".{args.phase}.pending-", dir=artifacts_root)
        )
        os.chmod(staging_dir, 0o700)
        for name, raw in payloads:
            write_artifact(raw, staging_dir / name)
        directory = os.open(staging_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        os.replace(staging_dir, phase_dir)
        digest = durable_fact(fact_path, fact)
        for directory_path in (phase_dir, artifacts_root, facts_dir, session_dir):
            directory = os.open(directory_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        print(json.dumps({"fact_sha256": digest, "phase": args.phase, "session_sha256": hashlib.sha256(session_raw).hexdigest(), "status": "passed"}, sort_keys=True))
        return 0
    except (FactError, EvidenceError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
