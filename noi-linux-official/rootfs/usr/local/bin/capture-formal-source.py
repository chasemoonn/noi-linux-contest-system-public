#!/usr/bin/env python3
"""Capture one stable, single-link CSP source without following symlinks."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import sys


STABLE_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _same(left: os.stat_result, right: os.stat_result) -> bool:
    return all(getattr(left, field) == getattr(right, field) for field in STABLE_FIELDS)


def _open_directory(name: str, parent_fd: int | None = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(name, flags) if parent_fd is None else os.open(
        name, flags, dir_fd=parent_fd
    )
    current = os.fstat(fd)
    if not stat.S_ISDIR(current.st_mode):
        os.close(fd)
        raise RuntimeError("formal source component is not a directory")
    return fd


def _capture_tree(answer_root: str, candidate: str, problem: str, maximum: int) -> dict:
    if not re.fullmatch(r"CSP[0-9]{3}", candidate):
        raise ValueError("invalid candidate id")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", problem):
        raise ValueError("invalid problem slug")
    if maximum < 1 or maximum > 16 * 1024 * 1024:
        raise ValueError("invalid source size limit")

    root_fd = candidate_fd = problem_fd = source_fd = None
    try:
        root_fd = _open_directory(answer_root)
        candidate_fd = _open_directory(candidate, root_fd)
        problem_fd = _open_directory(problem, candidate_fd)
        root_before = os.fstat(root_fd)
        candidate_before = os.fstat(candidate_fd)
        problem_before = os.fstat(problem_fd)
        source_fd = os.open(
            f"{problem}.cpp", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=problem_fd
        )
        source_before = os.fstat(source_fd)
        if not stat.S_ISREG(source_before.st_mode) or source_before.st_nlink != 1:
            raise RuntimeError("formal source is not a single-link regular file")
        if source_before.st_size < 1 or source_before.st_size > maximum:
            raise RuntimeError("formal source size is invalid")

        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(source_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)

        source_after = os.fstat(source_fd)
        root_after = os.fstat(root_fd)
        candidate_after = os.fstat(candidate_fd)
        problem_after = os.fstat(problem_fd)
        root_path = os.stat(answer_root, follow_symlinks=False)
        candidate_path = os.stat(candidate, dir_fd=root_fd, follow_symlinks=False)
        problem_path = os.stat(problem, dir_fd=candidate_fd, follow_symlinks=False)
        source_path = os.stat(
            f"{problem}.cpp", dir_fd=problem_fd, follow_symlinks=False
        )
        for left, right in (
            (root_before, root_after),
            (root_after, root_path),
            (candidate_before, candidate_after),
            (candidate_after, candidate_path),
            (problem_before, problem_after),
            (problem_after, problem_path),
            (source_before, source_after),
            (source_after, source_path),
        ):
            if not _same(left, right):
                raise RuntimeError("formal source path changed while being captured")
        if (
            len(payload) != source_before.st_size
            or not payload
            or len(payload) > maximum
        ):
            raise RuntimeError("formal source read was incomplete")
        return {
            "schema": 1,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "base64": base64.b64encode(payload).decode("ascii"),
        }
    finally:
        for fd in (source_fd, problem_fd, candidate_fd, root_fd):
            if fd is not None:
                os.close(fd)


def capture(answer_root: str, candidate: str, problem: str, maximum: int) -> dict:
    if answer_root != "/home/student/答案":
        raise ValueError("unexpected answer root")
    return _capture_tree(answer_root, candidate, problem, maximum)


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        raise SystemExit("usage: capture-formal-source.py ROOT CANDIDATE PROBLEM MAXIMUM")
    snapshot = capture(argv[1], argv[2], argv[3], int(argv[4]))
    print(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
