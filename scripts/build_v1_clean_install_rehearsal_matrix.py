#!/usr/bin/env python3
"""Build the 13-scenario clean-install qualification matrix from sealed observations."""
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
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import apply_v1_clean_install as clean
import collect_v1_clean_install_rehearsal_observation as observation
from collect_v1_components import validate_role_component
from stage_v1_source_release import candidate_identity
from verify_v1_clean_install_rehearsal import MARKER, PHASES, validate as validate_matrix
from verify_v1_install_backup import safe_directory, safe_file


HEX64 = re.compile(r"^[a-f0-9]{64}$")


class MatrixBuildError(RuntimeError):
    pass


def trusted_self() -> None:
    requested = Path(os.path.abspath(__file__)); metadata = os.lstat(requested)
    if requested != requested.resolve(strict=True) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise MatrixBuildError("clean install matrix builder metadata is unsafe")
    if platform.system().lower() == "linux":
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise MatrixBuildError("clean install matrix builder is not trusted")
        current = requested.parent
        while True:
            row = os.lstat(current)
            if stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode) or row.st_uid != 0 \
                    or stat.S_IMODE(row.st_mode) & 0o022:
                raise MatrixBuildError("clean install matrix builder ancestor is unsafe")
            if current.parent == current: break
            current = current.parent


def read_json(path: Path, *, maximum: int = 4 * 1024 * 1024) -> tuple[dict, bytes]:
    safe_directory(Path(os.path.abspath(path)).parent)
    raw, metadata = safe_file(path, maximum=maximum)
    if platform.system().lower() == "linux" and (
            metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise MatrixBuildError("clean install matrix input is not root-owned")
    try: value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MatrixBuildError("clean install matrix input is invalid JSON") from exc
    if not isinstance(value, dict):
        raise MatrixBuildError("clean install matrix input root differs")
    return value, raw


def system_file(path: Path, maximum: int) -> bytes:
    requested = Path(os.path.abspath(path)); resolved = requested.resolve(strict=True)
    current = Path(resolved.anchor)
    for part in resolved.parts[1:-1]:
        current = current / part; ancestor = os.lstat(current)
        if stat.S_ISLNK(ancestor.st_mode) or not stat.S_ISDIR(ancestor.st_mode) \
                or ancestor.st_uid != 0 or stat.S_IMODE(ancestor.st_mode) & 0o022:
            raise MatrixBuildError("clean install matrix host identity ancestor differs")
    descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or not 0 < metadata.st_size <= maximum \
                or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise MatrixBuildError("clean install matrix host identity differs")
        raw = os.read(descriptor, metadata.st_size + 1)
        if len(raw) != metadata.st_size:
            raise MatrixBuildError("clean install matrix host identity changed")
        return raw
    finally: os.close(descriptor)


def host(session_id: str) -> dict:
    machine = system_file(Path("/etc/machine-id"), 4096).strip()
    release = system_file(Path("/etc/os-release"), 1024 * 1024)
    if not machine:
        raise MatrixBuildError("clean install matrix machine identity is empty")
    return {"anonymous_id": hashlib.sha256(session_id.encode("ascii") + b":" + machine).hexdigest(),
            "architecture": platform.machine(), "kernel": platform.release(),
            "os_release_sha256": hashlib.sha256(release).hexdigest()}


def component_set(control_path: Path, desktop_path: Path, oj_path: Path,
                  source_revision: str, controller_image: str) -> dict:
    control = validate_role_component(read_json(control_path)[0], "control")
    desktop = validate_role_component(read_json(desktop_path)[0], "desktop")
    oj = validate_role_component(read_json(oj_path)[0], "oj")
    result = {"orchestrator_image_digest": control["orchestrator_image_digest"],
              "desktop_image_id": desktop["desktop_image_id"],
              "desktop_source_revision": desktop["desktop_source_revision"],
              "hydro_plugin_sha256": oj["hydro_plugin_sha256"]}
    if result["orchestrator_image_digest"] != controller_image \
            or result["desktop_source_revision"] != source_revision:
        raise MatrixBuildError("clean install matrix component binding differs")
    return result


def expected_directories() -> list[str]:
    return ["success"] + [f"phase_failure-{phase}" for phase in PHASES] \
        + [f"power_loss-{phase}" for phase in PHASES]


def load_observations(root: Path, plan: dict) -> list[dict]:
    directory = safe_directory(root)
    names = expected_directories()
    if {path.name for path in directory.iterdir()} != set(names):
        raise MatrixBuildError("clean install observation directory set differs")
    result = []
    for name in names:
        current = safe_directory(directory / name)
        value, _ = read_json(current / "observation.json")
        case_plan = {"plan_id": value.get("plan_id"),
                     "backup_manifest_sha256": value.get("backup_manifest_sha256")}
        observation.validate_document(value, current, case_plan)
        expected_kind, expected_phase = ("success", None) if name == "success" else name.split("-", 1)
        if expected_kind == "phase_failure": expected_phase = name[len("phase_failure-"):]
        elif expected_kind == "power_loss": expected_phase = name[len("power_loss-"):]
        if value["kind"] != expected_kind or value["phase"] != expected_phase \
                or value["plan_id"] != plan["plan_id"]:
            raise MatrixBuildError("clean install observation identity differs")
        result.append(value)
    return result


def scenario(value: dict) -> dict:
    artifacts = value["artifacts"]
    result = dict(value["result"])
    result.update({"private_plan_sha256": value["private_plan_sha256"],
                   "backup_manifest_sha256": value["backup_manifest_sha256"],
                   "fresh_baseline_sha256": artifacts["fresh_baseline"]["sha256"],
                   "ordinary_oj_before_sha256": artifacts["ordinary_before"]["sha256"],
                   "ordinary_oj_after_sha256": artifacts["ordinary_after"]["sha256"],
                   "execution_log_sha256": artifacts["execution_log"]["sha256"],
                   "terminal_receipt_sha256": artifacts["terminal_receipt"]["sha256"]})
    if value["kind"] != "success":
        result = {"kind": value["kind"], "phase": value["phase"], **result}
    return result


def assemble(source: dict, components: dict, plan: dict, observations: list[dict],
             session_id: str, qualification_marker: str, host_value: dict,
             observed_at: str) -> dict:
    success = [value for value in observations if value["kind"] == "success"]
    rollbacks = [value for value in observations if value["kind"] != "success"]
    if len(success) != 1 or len(rollbacks) != 12:
        raise MatrixBuildError("clean install observation coverage differs")
    document = {"$schema": "v1-clean-install-rehearsal-matrix.schema.json", "schema_version": 2,
        "source": source, "components": components,
        "plan": {"plan_id": plan["plan_id"]},
        "session_id": session_id, "qualification_marker": qualification_marker,
        "observed_at": observed_at, "host": host_value, "success": scenario(success[0]),
        "rollback_scenarios": [scenario(value) for value in rollbacks]}
    return validate_matrix(document, expected_revision=source["revision"],
                           expected_tree=source["tree"], expected_components=components)


def build(args) -> dict:
    if not HEX64.fullmatch(args.expected_plan_sha256) or not MARKER.fullmatch(args.qualification_marker):
        raise MatrixBuildError("clean install matrix trust identity differs")
    plan = clean.load_plan(args.private_plan, args.expected_plan_sha256)
    if plan.get("scope") != "qualification-lab":
        raise MatrixBuildError("clean install matrix requires a qualification-lab plan")
    manifest, _, candidate = candidate_identity(Path(plan["candidate"]), plan["candidate_manifest_sha256"],
                                                 require_production=False)
    source = {"revision": candidate["revision"], "tree": candidate["tree"]}
    expected_release = f"{source['revision']}-{plan['candidate_manifest_sha256'][:12]}"
    if plan["source_release"] != expected_release or manifest["source"]["tree"] != source["tree"]:
        raise MatrixBuildError("clean install matrix source binding differs")
    contract = read_json(Path(plan["expected_contract"]))[0]
    components = component_set(args.control_component, args.desktop_component, args.oj_component,
                               source["revision"], contract.get("controller_image_id"))
    observations = load_observations(args.observations_root, plan)
    session_id = secrets.token_hex(32)
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    document = assemble(source, components, plan, observations, session_id,
                        args.qualification_marker, host(session_id), observed_at)
    output = Path(os.path.abspath(args.output)); safe_directory(output.parent)
    observation.atomic(output, observation.canonical(document))
    return {"status": "sealed", "scenarios": 13, "session_id": session_id,
            "matrix_sha256": hashlib.sha256(observation.canonical(document)).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--private-plan", required=True, type=Path)
    parser.add_argument("--expected-plan-sha256", required=True); parser.add_argument("--observations-root", required=True, type=Path)
    parser.add_argument("--control-component", required=True, type=Path); parser.add_argument("--desktop-component", required=True, type=Path)
    parser.add_argument("--oj-component", required=True, type=Path); parser.add_argument("--qualification-marker", required=True)
    parser.add_argument("--output", required=True, type=Path); args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise MatrixBuildError("clean install matrix builder requires Linux root")
        trusted_self(); clean.trusted_self(); print(json.dumps(build(args), sort_keys=True)); return 0
    except (MatrixBuildError, observation.ObservationError, clean.ApplyInstallError,
            OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
