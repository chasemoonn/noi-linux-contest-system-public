#!/usr/bin/env python3
"""Validate the machine-produced clean-install success/failure/power-loss matrix."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import sys


HEX40 = re.compile(r"^[a-f0-9]{40}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
MARKER = re.compile(r"^NOI-V1-QUAL-[A-Z0-9]{16,64}$")
PHASES = (
    "source_release", "clean_materials", "hydro_integration",
    "closed_frontend", "controller", "post_install_verification",
)


class RehearsalError(ValueError):
    pass


def exact(value, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise RehearsalError(f"{label} field set differs")
    return value


def hex64(value, label: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise RehearsalError(f"{label} differs")
    return value


def timestamp(value) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RehearsalError("clean install rehearsal timestamp differs")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RehearsalError("clean install rehearsal timestamp differs") from exc
    return value


def validate(document: dict, *, expected_revision: str | None = None,
             expected_tree: str | None = None, expected_components: dict | None = None) -> dict:
    row = exact(document, {"$schema", "schema_version", "source", "components", "plan", "session_id",
        "qualification_marker", "observed_at", "host", "success", "rollback_scenarios"}, "rehearsal")
    if row["$schema"] != "v1-clean-install-rehearsal-matrix.schema.json" or row["schema_version"] != 2:
        raise RehearsalError("clean install rehearsal identity differs")
    source = exact(row["source"], {"revision", "tree"}, "source")
    if not HEX40.fullmatch(str(source["revision"])) or not HEX40.fullmatch(str(source["tree"])):
        raise RehearsalError("clean install rehearsal source differs")
    if expected_revision is not None and source["revision"] != expected_revision:
        raise RehearsalError("clean install rehearsal revision differs")
    if expected_tree is not None and source["tree"] != expected_tree:
        raise RehearsalError("clean install rehearsal tree differs")
    components = exact(row["components"], {"orchestrator_image_digest", "desktop_image_id",
        "desktop_source_revision", "hydro_plugin_sha256"}, "components")
    if not DIGEST.fullmatch(str(components["orchestrator_image_digest"])) \
            or not DIGEST.fullmatch(str(components["desktop_image_id"])) \
            or not HEX40.fullmatch(str(components["desktop_source_revision"])) \
            or not HEX64.fullmatch(str(components["hydro_plugin_sha256"])) \
            or components["desktop_source_revision"] != source["revision"]:
        raise RehearsalError("clean install rehearsal components differ")
    if expected_components is not None and components != expected_components:
        raise RehearsalError("clean install rehearsal expected components differ")
    plan = exact(row["plan"], {"plan_id"}, "plan")
    hex64(plan["plan_id"], "plan ID")
    hex64(row["session_id"], "session ID")
    if not isinstance(row["qualification_marker"], str) or not MARKER.fullmatch(row["qualification_marker"]):
        raise RehearsalError("clean install rehearsal marker differs")
    timestamp(row["observed_at"])
    host = exact(row["host"], {"anonymous_id", "architecture", "kernel", "os_release_sha256"}, "host")
    if not isinstance(host["architecture"], str) or not host["architecture"] \
            or not isinstance(host["kernel"], str) or not host["kernel"]:
        raise RehearsalError("clean install rehearsal host differs")
    hex64(host["anonymous_id"], "host anonymous ID"); hex64(host["os_release_sha256"], "OS release SHA256")

    success = exact(row["success"], {"terminal", "controller_healthy", "closed_frontend", "active_seats",
        "managed_rules", "cloud_state", "pending_markers", "ordinary_oj_errors", "ordinary_oj_restarts",
        "ordinary_oj_pid_changes", "fresh_baseline_sha256", "ordinary_oj_before_sha256",
        "ordinary_oj_after_sha256", "execution_log_sha256", "terminal_receipt_sha256",
        "private_plan_sha256", "backup_manifest_sha256"},
        "success")
    if success["terminal"] != "committed" or success["controller_healthy"] is not True \
            or success["closed_frontend"] is not True or success["cloud_state"] != "STOPPED" \
            or any(success[key] != 0 for key in ("active_seats", "managed_rules", "pending_markers",
                "ordinary_oj_errors", "ordinary_oj_restarts", "ordinary_oj_pid_changes")):
        raise RehearsalError("clean install committed scenario differs")
    for key in ("private_plan_sha256", "backup_manifest_sha256", "fresh_baseline_sha256",
                "ordinary_oj_before_sha256", "ordinary_oj_after_sha256", "execution_log_sha256",
                "terminal_receipt_sha256"):
        hex64(success[key], f"success {key}")
    if success["fresh_baseline_sha256"] != success["backup_manifest_sha256"]:
        raise RehearsalError("clean install success baseline binding differs")

    scenarios = row["rollback_scenarios"]
    if not isinstance(scenarios, list) or len(scenarios) != len(PHASES) * 2:
        raise RehearsalError("clean install rollback matrix size differs")
    observed = set()
    scenario_keys = {"kind", "phase", "terminal", "clean_target", "caddy_restored", "hydro_restored",
        "controller_absent", "cloud_state", "pending_markers", "ordinary_oj_errors", "ordinary_oj_restarts",
        "ordinary_oj_pid_changes", "fresh_baseline_sha256", "ordinary_oj_before_sha256",
        "ordinary_oj_after_sha256", "execution_log_sha256", "terminal_receipt_sha256",
        "private_plan_sha256", "backup_manifest_sha256"}
    for index, item in enumerate(scenarios):
        scenario = exact(item, scenario_keys, f"rollback scenario {index}")
        identity = (scenario["kind"], scenario["phase"])
        if scenario["kind"] not in {"phase_failure", "power_loss"} or scenario["phase"] not in PHASES \
                or identity in observed:
            raise RehearsalError("clean install rollback scenario identity differs")
        observed.add(identity)
        if scenario["terminal"] != "rollback_verified" or scenario["clean_target"] is not True \
                or scenario["caddy_restored"] is not True or scenario["hydro_restored"] is not True \
                or scenario["controller_absent"] is not True or scenario["cloud_state"] != "STOPPED" \
                or any(scenario[key] != 0 for key in ("pending_markers", "ordinary_oj_errors",
                    "ordinary_oj_restarts", "ordinary_oj_pid_changes")):
            raise RehearsalError("clean install rollback scenario result differs")
        for key in ("private_plan_sha256", "backup_manifest_sha256", "fresh_baseline_sha256",
                    "ordinary_oj_before_sha256", "ordinary_oj_after_sha256", "execution_log_sha256",
                    "terminal_receipt_sha256"):
            hex64(scenario[key], f"rollback scenario {key}")
        if scenario["fresh_baseline_sha256"] != scenario["backup_manifest_sha256"]:
            raise RehearsalError("clean install rollback baseline binding differs")
    expected = {(kind, phase) for kind in ("phase_failure", "power_loss") for phase in PHASES}
    if observed != expected:
        raise RehearsalError("clean install rollback matrix is incomplete")
    return row


def read(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or not 0 < metadata.st_size <= 4 * 1024 * 1024:
            raise RehearsalError("clean install rehearsal file is unsafe")
        raw = os.read(descriptor, metadata.st_size + 1)
        if len(raw) != metadata.st_size:
            raise RehearsalError("clean install rehearsal file changed while reading")
    finally:
        os.close(descriptor)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RehearsalError("clean install rehearsal is invalid JSON") from exc


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("matrix", type=Path); args = parser.parse_args()
    try:
        validate(read(args.matrix)); print(json.dumps({"status": "passed", "scenarios": 13}, sort_keys=True)); return 0
    except (OSError, RehearsalError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
