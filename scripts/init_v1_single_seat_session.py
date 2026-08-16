#!/usr/bin/env python3
"""Initialize one root-only synthetic single-seat qualification session."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import sys

from verify_v1_single_seat_evidence import (
    EvidenceError,
    validate_components,
    validate_context,
    validate_source,
)
from collect_v1_components import ComponentError, validate_role_component


ROOT = Path(__file__).resolve().parents[1]
SAFE_DIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SessionError(RuntimeError):
    pass


def read_json_regular(
    path: Path, label: str, limit: int = 1024 * 1024
) -> tuple[dict, bytes]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SessionError(f"cannot open {label} safely: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > limit
        ):
            raise SessionError(f"{label} must be a bounded single-link regular file")
        raw = b""
        while len(raw) <= limit:
            block = os.read(descriptor, 65536)
            if not block:
                break
            raw += block
        if len(raw) != info.st_size:
            raise SessionError(f"{label} changed while reading")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SessionError(f"{label} must be an object")
    return value, raw


def git(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SessionError("Git identity check could not complete") from exc
    value = result.stdout.strip()
    if result.returncode or (arguments[:1] != ("status",) and not value):
        raise SessionError("Git identity check failed")
    return value


def anonymous(session_id: str, value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise SessionError("private context identifier is invalid")
    return hashlib.sha256(f"{session_id}:{value}".encode("utf-8")).hexdigest()


def build_session(
    *,
    session_id: str,
    source: dict,
    components: dict,
    private_context: dict,
    component_facts: dict,
    created_at: str,
) -> dict:
    if set(private_context) != {
        "candidate_id",
        "contest_id",
        "cutoff_at_ms",
        "problem_slug",
        "seat_candidate",
        "seat_id",
    }:
        raise SessionError("private context shape differs")
    context = {
        "contest_id_sha256": anonymous(session_id, private_context["contest_id"]),
        "seat_id_sha256": anonymous(session_id, private_context["seat_id"]),
        "candidate_id": private_context["candidate_id"],
        "seat_candidate": private_context["seat_candidate"],
        "problem_slug": private_context["problem_slug"],
        "cutoff_at_ms": private_context["cutoff_at_ms"],
    }
    try:
        validate_source(source)
        validate_components(components)
        validate_context(context)
    except EvidenceError as exc:
        raise SessionError(str(exc)) from exc
    if components["desktop_source_revision"] != source["revision"]:
        raise SessionError("desktop source revision differs")
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise SessionError("created_at is invalid")
    if not isinstance(component_facts, dict) or set(component_facts) != {
        "control",
        "desktop",
        "oj",
    }:
        raise SessionError("component fact digests differ")
    if any(
        not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
        for digest in component_facts.values()
    ):
        raise SessionError("component fact digest is invalid")
    expected_component_facts = {
        role: {
            "reference": f"components/{role}.json",
            "sha256": component_facts[role],
        }
        for role in ("control", "desktop", "oj")
    }
    return {
        "$schema": "v1-single-seat-session.schema.json",
        "schema_version": 1,
        "session_id": session_id,
        "created_at": created_at,
        "source": source,
        "components": components,
        "component_facts": expected_component_facts,
        "context": context,
    }


def merge_role_components(control: dict, desktop: dict, oj: dict) -> dict:
    try:
        validate_role_component(control, "control")
        validate_role_component(desktop, "desktop")
        validate_role_component(oj, "oj")
    except ComponentError as exc:
        raise SessionError(str(exc)) from exc
    return {
        "orchestrator_image_digest": control["orchestrator_image_digest"],
        "desktop_image_id": desktop["desktop_image_id"],
        "desktop_source_revision": desktop["desktop_source_revision"],
        "hydro_plugin_sha256": oj["hydro_plugin_sha256"],
    }


def require_fresh_component_facts(rows: list[dict], created_at: datetime) -> None:
    for row in rows:
        observed_at = datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
        age = (created_at - observed_at).total_seconds()
        if age < 0 or age > 120:
            raise SessionError(
                "component observations must precede session creation by at most 120 seconds"
            )


def durable_new_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def require_root_private_path(path: Path, label: str) -> None:
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    if resolved != requested or not resolved.is_dir() or requested.is_symlink():
        raise SessionError(f"{label} must be a real canonical directory")
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        current = current / part
        info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise SessionError(f"{label} has an unsafe ancestor: {current}")
    if stat.S_IMODE(resolved.stat().st_mode) & 0o077:
        raise SessionError(f"{label} must be mode 0700 or stricter")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-components", type=Path, required=True)
    parser.add_argument("--desktop-components", type=Path, required=True)
    parser.add_argument("--oj-components", type=Path, required=True)
    parser.add_argument("--private-context", type=Path, required=True)
    parser.add_argument("--output-parent", type=Path, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise SessionError("single-seat session initialization requires Linux root")
        if not SAFE_DIR.fullmatch(args.name):
            raise SessionError("session directory name is unsafe")
        if git("status", "--porcelain=v1", "--untracked-files=no"):
            raise SessionError("tracked Git worktree must be clean")
        source = {"revision": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")}
        control_components, control_raw = read_json_regular(
            Path(os.path.abspath(args.control_components)), "control component fact"
        )
        desktop_components, desktop_raw = read_json_regular(
            Path(os.path.abspath(args.desktop_components)), "desktop component fact"
        )
        oj_components, oj_raw = read_json_regular(
            Path(os.path.abspath(args.oj_components)), "OJ component fact"
        )
        components = merge_role_components(control_components, desktop_components, oj_components)
        private_context, _ = read_json_regular(
            Path(os.path.abspath(args.private_context)), "private context"
        )
        session_id = secrets.token_hex(32)
        created = datetime.now(timezone.utc)
        require_fresh_component_facts(
            [control_components, desktop_components, oj_components], created
        )
        component_payloads = {
            "control": control_raw,
            "desktop": desktop_raw,
            "oj": oj_raw,
        }
        component_digests = {
            role: hashlib.sha256(raw).hexdigest()
            for role, raw in component_payloads.items()
        }
        document = build_session(
            session_id=session_id,
            source=source,
            components=components,
            private_context=private_context,
            component_facts=component_digests,
            created_at=created.isoformat().replace("+00:00", "Z"),
        )
        parent = Path(os.path.abspath(args.output_parent))
        require_root_private_path(parent, "output parent")
        session = parent / args.name
        session.mkdir(mode=0o700)
        try:
            (session / "facts").mkdir(mode=0o700)
            (session / "artifacts").mkdir(mode=0o700)
            (session / "components").mkdir(mode=0o700)
            for role, raw in component_payloads.items():
                durable_new_file(session / "components" / f"{role}.json", raw)
            payload = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            durable_new_file(session / "session.json", payload)
            for directory_path in (session / "components", session):
                directory = os.open(
                    directory_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except Exception:
            # The new session contains no external state; leave partial evidence
            # visible for the operator rather than deleting an ambiguous tree.
            raise
        print(json.dumps({"session": str(session), "session_sha256": hashlib.sha256(payload).hexdigest(), "status": "initialized"}, sort_keys=True))
        return 0
    except (SessionError, EvidenceError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
