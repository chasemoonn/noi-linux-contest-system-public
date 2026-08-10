#!/usr/bin/env python3
"""Fail closed when public source contains private runtime material.

This checker intentionally scans both tracked files and non-ignored untracked
files.  That makes it useful before the first commit as well as in CI.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MAX_PUBLIC_FILE_BYTES = 10 * 1024 * 1024
BLOCKED_SUFFIXES = (
    ".zip",
    ".tgz",
    ".tar",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".iso",
    ".img",
    ".raw",
    ".qcow",
    ".qcow2",
    ".vdi",
    ".vhd",
    ".vhdx",
    ".vmdk",
    ".ova",
    ".ovf",
    ".wim",
    ".squashfs",
    ".oci",
    ".oci.tar",
    ".oci.tar.zst",
    ".tar.zst",
)
BLOCKED_LOG_SUFFIXES = (
    ".log",
    ".jsonl",
)
BLOCKED_BACKUP_SUFFIXES = (
    ".bak",
    ".backup",
    ".dump",
    ".dump.gz",
    ".sql",
    ".sql.gz",
    ".sql.bz2",
    ".sql.xz",
    ".sql.zst",
    ".bson",
    ".bson.gz",
)
BLOCKED_BASENAMES = {
    ".env",
    "config.yaml",
    "known_hosts",
    "orchestrator.db",
}
SECRET_PATTERNS = (
    (
        "PRIVATE_KEY",
        re.compile(
            rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----"
        ),
    ),
    ("GITHUB_TOKEN", re.compile(rb"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}")),
    ("GITHUB_PAT", re.compile(rb"github_pat_[A-Za-z0-9_]{40,}")),
    ("ALIYUN_ACCESS_KEY", re.compile(rb"LTAI[A-Za-z0-9]{12,}")),
    ("AWS_ACCESS_KEY", re.compile(rb"AKIA[A-Z0-9]{16}")),
    ("TENCENT_SECRET_ID", re.compile(rb"AKID[A-Za-z0-9]{20,}")),
    ("PINNED_HOST_FINGERPRINT", re.compile(rb"SHA256:[A-Za-z0-9+/]{43}=?")),
    (
        "CREDENTIAL_IN_MONGODB_URI",
        re.compile(
            rb"mongodb(?:\+srv)?://[^\s/:{}]+:[^\s/@{}]+@", re.IGNORECASE
        ),
    ),
)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
IPV4_LITERAL = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
PRIVATE_SITE_MARKERS = (
    "quxi" + "nao",
    "xwje" + "du",
)


def windows_reparse_point(metadata: os.stat_result) -> bool:
    """Return true for NTFS junctions and other Windows reparse objects."""
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(flag and attributes & flag)


def unsafe_link_component(path: Path) -> tuple[str, Path] | None:
    """Find a link-like component without traversing past it.

    A Git worktree can contain an NTFS junction that ``Path.is_symlink()``
    reports as a normal directory.  Check every lexical component from ROOT to
    the candidate before the scanner opens the candidate itself.
    """
    candidate = ROOT
    for part in path.relative_to(ROOT).parts:
        candidate /= part
        metadata = os.lstat(candidate)
        if stat.S_ISLNK(metadata.st_mode):
            return "UNSAFE_PUBLIC_SYMLINK", candidate
        if windows_reparse_point(metadata):
            return "UNSAFE_PUBLIC_REPARSE_POINT", candidate
    return None


def git_paths() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths: set[Path] = set()
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8", errors="strict"))
        path = ROOT / relative
        if os.path.lexists(path):
            unsafe = unsafe_link_component(path)
            paths.add(unsafe[1] if unsafe else path)
    return sorted(paths, key=lambda value: value.as_posix())


def exported_tree_paths() -> list[Path]:
    """Enumerate an exported source tree without following links.

    GitHub source archives and ``git archive`` exports intentionally omit
    ``.git``.  Those are supported inputs too, but every object shipped in the
    export must remain visible to the checker instead of being filtered by a
    local ignore file.
    """
    paths: list[Path] = []
    pending = [ROOT]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    paths.append(path)
                elif windows_reparse_point(entry.stat(follow_symlinks=False)):
                    # NTFS junctions are directories but are not reported by
                    # is_symlink().  Include the junction itself for a clear
                    # failure and never enqueue or traverse its target.
                    paths.append(path)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                else:
                    # Include regular and special files.  scan() rejects the
                    # latter before attempting to read from them.
                    paths.append(path)
    return sorted(paths, key=lambda value: value.as_posix())


def source_paths() -> list[Path]:
    if (ROOT / ".git").exists():
        return git_paths()
    return exported_tree_paths()


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def blocked_runtime_file(path: Path) -> str | None:
    lower = path.name.lower()
    rel = relative(path).lower()
    if lower in BLOCKED_BASENAMES:
        return "runtime configuration/state file"
    if lower.endswith(BLOCKED_LOG_SUFFIXES):
        return "runtime log/event stream"
    if lower.endswith((".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3")):
        return "credential or runtime database"
    if lower.endswith(BLOCKED_BACKUP_SUFFIXES):
        return "runtime backup/export"
    if any(lower.endswith(suffix) for suffix in BLOCKED_SUFFIXES):
        return "disk/container image artifact"
    if rel.startswith("local-release/"):
        return "local image bundle"
    return None


def check_markdown_links(path: Path, text: str, failures: list[dict[str, str]]) -> None:
    without_fences = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    without_code = re.sub(r"`[^`\n]*`", "", without_fences)
    for match in MARKDOWN_LINK.finditer(without_code):
        raw = match.group(1).strip()
        if raw.startswith("<") and raw.endswith(">"):
            raw = raw[1:-1]
        target = raw.split("#", 1)[0].split("?", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        if target.startswith(("data:", "javascript:")):
            failures.append(
                {
                    "code": "UNSAFE_MARKDOWN_LINK",
                    "path": relative(path),
                    "detail": raw,
                }
            )
            continue
        resolved = (path.parent / unquote(target)).resolve()
        try:
            resolved.relative_to(ROOT.resolve())
        except ValueError:
            failures.append(
                {
                    "code": "MARKDOWN_LINK_ESCAPES_REPOSITORY",
                    "path": relative(path),
                    "detail": raw,
                }
            )
            continue
        if not resolved.exists():
            failures.append(
                {
                    "code": "BROKEN_MARKDOWN_LINK",
                    "path": relative(path),
                    "detail": raw,
                }
            )


def scan() -> dict[str, object]:
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    paths = source_paths()
    for path in paths:
        unsafe = unsafe_link_component(path)
        if unsafe:
            code, unsafe_path = unsafe
            failures.append(
                {
                    "code": code,
                    "path": relative(unsafe_path),
                    "detail": "public source must contain regular files only",
                }
            )
            continue
        rel = relative(path)
        mode = path.stat(follow_symlinks=False).st_mode
        if not stat.S_ISREG(mode):
            failures.append(
                {
                    "code": "UNSAFE_PUBLIC_SPECIAL_FILE",
                    "path": rel,
                    "detail": "public source must contain regular files only",
                }
            )
            continue
        for marker in PRIVATE_SITE_MARKERS:
            if marker in rel.lower():
                failures.append(
                    {
                        "code": "PRIVATE_SITE_PATH",
                        "path": rel,
                        "detail": "site-specific filename",
                    }
                )
        blocked = blocked_runtime_file(path)
        if blocked:
            failures.append(
                {"code": "BLOCKED_PUBLIC_FILE", "path": rel, "detail": blocked}
            )
            continue
        size = path.stat().st_size
        if size > MAX_PUBLIC_FILE_BYTES:
            failures.append(
                {
                    "code": "PUBLIC_FILE_TOO_LARGE",
                    "path": rel,
                    "detail": f"{size} bytes exceeds {MAX_PUBLIC_FILE_BYTES}",
                }
            )
            continue
        data = path.read_bytes()
        for code, pattern in SECRET_PATTERNS:
            if pattern.search(data):
                failures.append(
                    {"code": code, "path": rel, "detail": "high-confidence match"}
                )
        try:
            decoded = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            decoded = ""
        lowered = decoded.lower()
        for marker in PRIVATE_SITE_MARKERS:
            if marker in lowered:
                failures.append(
                    {
                        "code": "PRIVATE_SITE_MARKER",
                        "path": rel,
                        "detail": "site-specific identity",
                    }
                )
        for match in IPV4_LITERAL.finditer(decoded):
            try:
                address = ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            if address.is_global:
                failures.append(
                    {
                        "code": "PUBLIC_IPV4_LITERAL",
                        "path": rel,
                        "detail": "globally routable literal IPv4 address",
                    }
                )
        if path.suffix.lower() == ".md":
            if not decoded:
                failures.append(
                    {"code": "MARKDOWN_NOT_UTF8", "path": rel, "detail": "decode failed"}
                )
            else:
                check_markdown_links(path, decoded, failures)
        if b"\x00" in data and path.suffix.lower() not in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".woff",
            ".woff2",
        }:
            warnings.append(
                {"code": "UNEXPECTED_BINARY", "path": rel, "detail": "contains NUL"}
            )
    return {
        "schema_version": 1,
        "status": "ok" if not failures else "fail",
        "files_scanned": len(paths),
        "failures": failures,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check the public source tree for private or oversized artifacts."
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()
    try:
        report = scan()
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        report = {
            "schema_version": 1,
            "status": "error",
            "files_scanned": 0,
            "failures": [
                {"code": "CHECKER_ERROR", "path": "", "detail": str(exc)}
            ],
            "warnings": [],
        }
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(
            f"public-release-check: {report['status']} "
            f"files={report['files_scanned']} "
            f"failures={len(report['failures'])} "
            f"warnings={len(report['warnings'])}"
        )
        for item in report["failures"]:
            print(
                f"FAIL {item['code']} {item['path']}: {item['detail']}",
                file=sys.stderr,
            )
        for item in report["warnings"]:
            print(
                f"WARN {item['code']} {item['path']}: {item['detail']}",
                file=sys.stderr,
            )
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
