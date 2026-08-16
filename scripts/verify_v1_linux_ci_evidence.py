#!/usr/bin/env python3
"""Verify one non-secret V1 Linux CI evidence document."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from datetime import datetime, timezone


HEX40 = re.compile(r"^[a-f0-9]{40}$")
EXPECTED_GATES = [
    "python_compile",
    "python_unit_tests",
    "hydro_plugin_syntax",
    "hydro_plugin_tests",
    "submission_fault_injection",
    "deployment_shell_syntax",
    "demo_reproducibility",
    "v1_product_contract",
    "qualification_schema",
    "public_release_boundary",
]
HEX64 = re.compile(r"^[a-f0-9]{64}$")


class EvidenceError(ValueError):
    pass


def exact_keys(value, expected: set[str], label: str):
    if not isinstance(value, dict) or set(value) != expected:
        raise EvidenceError(f"{label} shape differs")
    return value


def validate(document: object, expected_revision: str | None = None) -> dict:
    root = exact_keys(
        document,
        {
            "$schema",
            "schema_version",
            "status",
            "source",
            "environment",
            "finished_at",
            "gates",
            "started_at",
        },
        "evidence",
    )
    if root["$schema"] != "v1-linux-ci-evidence.schema.json":
        raise EvidenceError("unexpected evidence schema")
    if root["schema_version"] != 1 or root["status"] != "passed":
        raise EvidenceError("evidence is not passed schema version 1")
    source = exact_keys(root["source"], {"revision", "tree"}, "source")
    if not HEX40.fullmatch(str(source["revision"])) or not HEX40.fullmatch(str(source["tree"])):
        raise EvidenceError("source revision or tree is invalid")
    if expected_revision is not None and source["revision"] != expected_revision:
        raise EvidenceError("source revision differs")
    environment = exact_keys(
        root["environment"],
        {"architecture", "effective_uid", "kernel", "node", "python", "system"},
        "environment",
    )
    if environment["system"] != "linux":
        raise EvidenceError("evidence was not produced on Linux")
    if environment["effective_uid"] != 0:
        raise EvidenceError("evidence was not produced by Linux root")
    for name in ("architecture", "kernel", "node", "python"):
        value = environment[name]
        if not isinstance(value, str) or not value or len(value) > 200:
            raise EvidenceError(f"environment.{name} is invalid")
    timestamps = []
    for name in ("started_at", "finished_at"):
        value = root[name]
        if not isinstance(value, str) or not value.endswith("Z"):
            raise EvidenceError(f"{name} is invalid")
        try:
            timestamps.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError as exc:
            raise EvidenceError(f"{name} is invalid") from exc
    if any(value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value) for value in timestamps):
        raise EvidenceError("timestamps must be UTC")
    if timestamps[1] < timestamps[0]:
        raise EvidenceError("finished_at precedes started_at")
    if not isinstance(root["gates"], list) or len(root["gates"]) != len(EXPECTED_GATES):
        raise EvidenceError("gate count differs")
    observed = []
    for index, value in enumerate(root["gates"]):
        row = exact_keys(
            value,
            {
                "duration_ms",
                "name",
                "status",
                "stderr_file",
                "stderr_sha256",
                "stdout_file",
                "stdout_sha256",
            },
            f"gates[{index}]",
        )
        if row["status"] != "passed":
            raise EvidenceError(f"gate did not pass: {row['name']}")
        if not isinstance(row["duration_ms"], int) or row["duration_ms"] < 0:
            raise EvidenceError(f"gate duration is invalid: {row['name']}")
        for stream in ("stderr_sha256", "stdout_sha256"):
            if not HEX64.fullmatch(str(row[stream])):
                raise EvidenceError(f"gate {stream} is invalid: {row['name']}")
        for stream in ("stderr_file", "stdout_file"):
            value = row[stream]
            expected = f"{index + 1:02d}-{row['name']}.{stream.removesuffix('_file')}.log"
            if value != expected or Path(value).name != value:
                raise EvidenceError(f"gate {stream} is invalid: {row['name']}")
        observed.append(row["name"])
    if observed != EXPECTED_GATES:
        raise EvidenceError("gate names or order differ")
    return root


def verify_logs(document: dict, log_directory: Path) -> None:
    directory = log_directory.resolve()
    if not directory.is_dir() or directory.is_symlink():
        raise EvidenceError("log directory is missing or unsafe")
    expected_files = set()
    for row in document["gates"]:
        for stream in ("stderr", "stdout"):
            name = row[f"{stream}_file"]
            expected_files.add(name)
            path = directory / name
            if not path.is_file() or path.is_symlink():
                raise EvidenceError(f"gate log is missing or unsafe: {name}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != row[f"{stream}_sha256"]:
                raise EvidenceError(f"gate log digest differs: {name}")
    observed_files = {path.name for path in directory.iterdir()}
    if observed_files != expected_files:
        raise EvidenceError("log directory contains unexpected entries")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--log-directory", type=Path, required=True)
    parser.add_argument("--expected-revision")
    args = parser.parse_args()
    try:
        if args.expected_revision is not None and not HEX40.fullmatch(args.expected_revision):
            raise EvidenceError("--expected-revision must be 40 lowercase hexadecimal characters")
        raw = args.evidence.read_bytes()
        document = json.loads(raw.decode("utf-8"))
        validated = validate(document, args.expected_revision)
        verify_logs(validated, args.log_directory)
        print(
            json.dumps(
                {
                    "evidence_sha256": hashlib.sha256(raw).hexdigest(),
                    "gates": len(validated["gates"]),
                    "revision": validated["source"]["revision"],
                    "status": "passed",
                },
                sort_keys=True,
            )
        )
        return 0
    except (EvidenceError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
