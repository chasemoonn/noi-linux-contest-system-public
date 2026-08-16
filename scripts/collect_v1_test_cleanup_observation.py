#!/usr/bin/env python3
"""Prove that one session-owned synthetic Hydro contest was deleted."""
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
import sys
import time

from bson import ObjectId
from pymongo import MongoClient


CONTEST_DOCTYPE = 30
DISCUSSION_DOCTYPE = 21
HEX64 = re.compile(r"^[a-f0-9]{64}$")


class CleanupError(RuntimeError):
    pass


def read_root_json(path: Path, label: str, limit: int = 1024 * 1024) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o077
            or not 0 < info.st_size <= limit
        ):
            raise CleanupError(f"{label} must be a bounded root-only single-link file")
        raw = b""
        while len(raw) <= limit:
            block = os.read(descriptor, min(65536, limit + 1 - len(raw)))
            if not block:
                break
            raw += block
        if len(raw) != info.st_size:
            raise CleanupError(f"{label} changed while reading")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CleanupError(f"{label} must be an object")
    return value


def contest_digest(session_id: str, contest_id: str) -> str:
    return hashlib.sha256(f"{session_id}:{contest_id}".encode("utf-8")).hexdigest()


def cleanup_counts(db, domain_id: str, tid: ObjectId) -> dict[str, int]:
    return {
        "contest": db["document"].count_documents(
            {"domainId": domain_id, "docType": CONTEST_DOCTYPE, "docId": tid}
        ),
        "registration_status": db["document.status"].count_documents(
            {"domainId": domain_id, "docType": CONTEST_DOCTYPE, "docId": tid}
        ),
        "discussion": db["document"].count_documents(
            {
                "domainId": domain_id,
                "docType": DISCUSSION_DOCTYPE,
                "parentType": CONTEST_DOCTYPE,
                "parentId": tid,
            }
        ),
        "scheduled_task": db["schedule"].count_documents(
            {"type": "schedule", "subType": "contest", "domainId": domain_id, "tid": tid}
        ),
        "linked_record": db["record"].count_documents(
            {"domainId": domain_id, "contest": tid}
        ),
    }


def build_observation(
    *, session_id: str, contest_id: str, counts: dict[str, int], observed_at_ms: int
) -> dict:
    expected_keys = {
        "contest",
        "discussion",
        "linked_record",
        "registration_status",
        "scheduled_task",
    }
    if set(counts) != expected_keys or any(
        isinstance(value, bool) or not isinstance(value, int) or value != 0
        for value in counts.values()
    ):
        raise CleanupError("synthetic contest cleanup is incomplete")
    if not HEX64.fullmatch(session_id):
        raise CleanupError("session ID is invalid")
    if not re.fullmatch(r"[a-f0-9]{24}", contest_id):
        raise CleanupError("contest ID is invalid")
    if isinstance(observed_at_ms, bool) or not isinstance(observed_at_ms, int) or observed_at_ms <= 0:
        raise CleanupError("cleanup timestamp is invalid")
    result = {
        "cleanup_verified_at_ms": observed_at_ms,
        "contest_absent": True,
        "contest_id_sha256": contest_digest(session_id, contest_id),
        "verification_method": "hydro_mongo_post_delete_absence",
        "discussion_count": counts["discussion"],
        "linked_record_count": counts["linked_record"],
        "registration_status_count": counts["registration_status"],
        "scheduled_task_count": counts["scheduled_task"],
    }
    result["cleanup_receipt_sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result


def durable_json(path: Path, document: dict) -> None:
    requested = Path(os.path.abspath(path))
    parent = requested.parent.resolve(strict=True)
    if parent != requested.parent or os.path.lexists(requested):
        raise CleanupError("output must be a new file in a canonical directory")
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
            raise CleanupError(f"output directory has an unsafe ancestor: {current}")
    if stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise CleanupError("output directory must be root-only")
    raw = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(requested, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--private-context", type=Path, required=True)
    parser.add_argument("--domain-id", default="system")
    parser.add_argument("--mongo-uri-env", default="HYDRO_MONGO_URI")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    client = None
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise CleanupError("test cleanup observation requires Linux root")
        session = read_root_json(Path(os.path.abspath(args.session)), "session")
        private = read_root_json(
            Path(os.path.abspath(args.private_context)), "private context"
        )
        if session.get("$schema") != "v1-single-seat-session.schema.json":
            raise CleanupError("session schema is invalid")
        session_id = str(session.get("session_id") or "")
        context = session.get("context")
        if not isinstance(context, dict):
            raise CleanupError("session context is invalid")
        if set(private) != {
            "candidate_id",
            "contest_id",
            "cutoff_at_ms",
            "problem_slug",
            "seat_candidate",
            "seat_id",
        }:
            raise CleanupError("private context shape differs")
        contest_id = str(private["contest_id"])
        if context.get("contest_id_sha256") != contest_digest(session_id, contest_id):
            raise CleanupError("private contest does not belong to this session")
        mongo_uri = os.environ.get(args.mongo_uri_env, "")
        if not mongo_uri:
            raise CleanupError("Mongo URI environment variable is missing")
        tid = ObjectId(contest_id)
        client = MongoClient(
            mongo_uri,
            tz_aware=True,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
        client.admin.command("ping")
        db = client.get_default_database()
        snapshots = []
        for index in range(3):
            snapshots.append(cleanup_counts(db, str(args.domain_id), tid))
            if index != 2:
                time.sleep(1)
        if any(value != snapshots[0] for value in snapshots[1:]):
            raise CleanupError("cleanup state was not stable across three observations")
        observation = build_observation(
            session_id=session_id,
            contest_id=contest_id,
            counts=snapshots[0],
            observed_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
        durable_json(Path(os.path.abspath(args.output)), observation)
        print(
            json.dumps(
                {
                    "cleanup_receipt_sha256": observation["cleanup_receipt_sha256"],
                    "status": "passed",
                },
                sort_keys=True,
            )
        )
        return 0
    except (CleanupError, OSError, ValueError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
