#!/usr/bin/env python3
"""Build one root-only, fully bound V1 controller upgrade plan."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import noictl
from apply_v1_controller import (
    Docker, LABEL_PLAN, LABEL_RELEASE, desired_definition as verify_desired_definition,
    inspect_matches, safe_ancestors, safe_docker_socket, safe_private_file,
)
from apply_v1_install import load_plan as verify_private_plan, verify_bindings
from stage_v1_source_release import candidate_identity, plan as source_plan
from verify_v1_controller_install_backup import validate_definition, validate_image
from verify_v1_install_backup import safe_directory, safe_file, validate_manifest
from verify_v1_qualification import validate_report


HEX64 = re.compile(r"^[a-f0-9]{64}$")
IMAGE = re.compile(r"^sha256:[a-f0-9]{64}$")


class PrivatePlanError(RuntimeError):
    pass


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def fsync_directory(path: Path) -> None:
    if platform.system().lower() != "linux":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def trusted_self() -> None:
    path = Path(os.path.abspath(__file__))
    metadata = os.lstat(path)
    if path != path.resolve(strict=True) or stat.S_ISLNK(metadata.st_mode) \
            or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise PrivatePlanError("private plan builder metadata is unsafe")
    if platform.system().lower() != "linux":
        return
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise PrivatePlanError("private plan builder is not trusted")
    current = path.parent
    while True:
        row = os.lstat(current)
        if stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode) \
                or row.st_uid != 0 or stat.S_IMODE(row.st_mode) & 0o022:
            raise PrivatePlanError("private plan builder ancestor is unsafe")
        if current.parent == current:
            break
        current = current.parent


def trusted_executable(path: Path) -> Path:
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    metadata = os.lstat(resolved)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or not os.access(resolved, os.X_OK):
        raise PrivatePlanError("private plan executable metadata is unsafe")
    if platform.system().lower() == "linux":
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise PrivatePlanError("private plan executable is not trusted")
        current = requested.parent
        while True:
            row = os.lstat(current)
            if row.st_uid != 0 or (not stat.S_ISLNK(row.st_mode) and
                    (not stat.S_ISDIR(row.st_mode) or stat.S_IMODE(row.st_mode) & 0o022)):
                raise PrivatePlanError("private plan executable ancestor is unsafe")
            if current.parent == current:
                break
            current = current.parent
    # Preserve the validated entry point in the plan.  Virtual-environment
    # interpreters are commonly symlinks whose invocation controls sys.prefix
    # and site-packages; serializing the resolved system binary loses that
    # isolation even though the target itself is trusted.
    return requested


STAGING_FILES = {"desired-config.yaml", "desired.env", "desired-controller-definition.json",
                 "post-install-contract.json", "private-upgrade-plan.json"}


def recover_staging(path: Path, allowed_files: set[str] | None = None) -> None:
    allowed = STAGING_FILES if allowed_files is None else set(allowed_files)
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) \
            or (platform.system().lower() == "linux" and
                (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700)):
        raise PrivatePlanError("incomplete private plan staging metadata differs")
    names = {item.name for item in path.iterdir()}
    if not names <= allowed | {"transaction"}:
        raise PrivatePlanError("incomplete private plan contains an unexpected entry")
    transaction = path / "transaction"
    if os.path.lexists(transaction):
        row = os.lstat(transaction)
        if stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode) \
                or any(transaction.iterdir()) \
                or (platform.system().lower() == "linux" and
                    (row.st_uid != 0 or stat.S_IMODE(row.st_mode) != 0o700)):
            raise PrivatePlanError("incomplete private plan transaction differs")
        os.rmdir(transaction)
    for name in sorted(names & allowed):
        target = path / name
        row = os.lstat(target)
        if stat.S_ISLNK(row.st_mode) or not stat.S_ISREG(row.st_mode) or row.st_nlink != 1 \
                or (platform.system().lower() == "linux" and
                    (row.st_uid != 0 or stat.S_IMODE(row.st_mode) != 0o600)):
            raise PrivatePlanError("incomplete private plan artifact differs")
        os.unlink(target)
    os.rmdir(path)
    fsync_directory(path.parent)


def private_staging(path: Path, plan_id: str, *, allowed_files: set[str] | None = None,
                    operation_slug: str = "upgrade") -> tuple[Path, Path]:
    allowed = STAGING_FILES if allowed_files is None else set(allowed_files)
    if not allowed or any(not isinstance(name, str) or not name or "/" in name or "\\" in name
                          for name in allowed) or not re.fullmatch(r"[a-z][a-z0-9-]*", operation_slug):
        raise PrivatePlanError("private plan staging contract is invalid")
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        raise PrivatePlanError("private plan output already exists")
    parent = requested.parent.resolve(strict=True)
    metadata = os.lstat(parent)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PrivatePlanError("private plan parent is unsafe")
    if platform.system().lower() == "linux" and (
        metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise PrivatePlanError("private plan parent is not root-only")
    staging = parent / f".{requested.name}.v1-{operation_slug}-{plan_id[:12]}.pending"
    if os.path.lexists(staging):
        recover_staging(staging, allowed)
    staging.mkdir(mode=0o700)
    if platform.system().lower() == "linux":
        os.chown(staging, 0, 0)
    os.chmod(staging, 0o700)
    fsync_directory(parent)
    return requested, staging.resolve(strict=True)


def publish_directory(staging: Path, final: Path) -> Path:
    if staging.parent != final.parent or os.path.lexists(final):
        raise PrivatePlanError("private plan publication target differs")
    os.replace(staging, final)
    fsync_directory(final.parent)
    resolved = final.resolve(strict=True)
    metadata = os.lstat(resolved)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) \
            or (platform.system().lower() == "linux" and
                (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700)):
        raise PrivatePlanError("published private plan directory differs")
    return resolved


def atomic(path: Path, raw: bytes, mode: int = 0o600) -> None:
    if os.path.lexists(path):
        raise PrivatePlanError(f"private plan artifact already exists: {path.name}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            os.chmod(temporary, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.lexists(temporary):
            os.unlink(temporary)


def exact_copy(source: Path, target: Path) -> bytes:
    raw, _ = safe_file(source, maximum=32 * 1024 * 1024)
    atomic(target, raw)
    copied, metadata = safe_private_file(target, target.name, maximum=32 * 1024 * 1024)
    if copied != raw or (platform.system().lower() == "linux" and
                         stat.S_IMODE(metadata.st_mode) != 0o600):
        raise PrivatePlanError("private plan copy differs")
    return raw


def load_backup(directory: Path, plan_id: str, manifest_sha256: str) -> tuple[Path, dict, dict]:
    root = safe_directory(directory)
    raw, _ = safe_file(root / "backup-manifest.json", maximum=4 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise PrivatePlanError("backup manifest trust pin differs")
    manifest = json.loads(raw.decode("utf-8"))
    validate_manifest(manifest, root, expected_plan_id=plan_id)
    definition = validate_definition(json.loads(
        safe_file(root / "controller-definition.json", maximum=4 * 1024 * 1024)[0].decode("utf-8")
    ))
    validate_image(json.loads(
        safe_file(root / "controller-image.json", maximum=4 * 1024 * 1024)[0].decode("utf-8")
    ), definition)
    if not definition["present"] or definition["container"]["running"] is not True:
        raise PrivatePlanError("upgrade requires one running controller backup")
    return root, manifest, definition


def runtime_environment(definition: dict) -> dict[str, str]:
    config = definition["container"]["immutable_identity"].get("config") or {}
    rows = config.get("Env")
    if not isinstance(rows, list):
        raise PrivatePlanError("controller environment is unavailable")
    result: dict[str, str] = {}
    for item in rows:
        if not isinstance(item, str) or "=" not in item or "\x00" in item:
            raise PrivatePlanError("controller environment row differs")
        name, value = item.split("=", 1)
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) or name in result:
            raise PrivatePlanError("controller environment keys differ")
        result[name] = value
    return result


def public_plan_identity(config: Path, environment: dict[str, str], candidate_row: dict,
                         scope: str) -> tuple[str, noictl.ConfigState]:
    previous = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(environment)
        state = noictl._load_config_state(config)
    finally:
        os.environ.clear()
        os.environ.update(previous)
    identity = {
        "schema_version": 1,
        "operation": "install",
        "scope": scope,
        "source_revision": candidate_row["revision"],
        "source_tree": candidate_row["tree"],
        "candidate_manifest_sha256": candidate_row["manifest_sha256"],
        "source_archive_sha256": candidate_row["archive_sha256"],
        "configuration_binding": noictl._installation_config_binding(state),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest(), state


def qualification_image(candidate: Path, manifest: dict, expected: str,
                        production: bool) -> str:
    if not IMAGE.fullmatch(expected):
        raise PrivatePlanError("controller image trust pin is invalid")
    qualification = manifest.get("qualification") or {}
    report_name = qualification.get("report")
    report_sha = qualification.get("report_sha256")
    if report_name is None:
        if production:
            raise PrivatePlanError("production candidate has no qualification report")
        return expected
    if not isinstance(report_name, str) or Path(report_name).name != report_name \
            or not HEX64.fullmatch(str(report_sha)):
        raise PrivatePlanError("qualification report identity differs")
    raw = noictl._safe_candidate_file(candidate / report_name, maximum=8 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != report_sha:
        raise PrivatePlanError("qualification report digest differs")
    report = validate_report(json.loads(raw.decode("utf-8")), require_qualified=production)
    if report["source_revision"] != manifest["source"]["revision"] \
            or report["components"]["orchestrator_image_digest"] != expected:
        raise PrivatePlanError("controller image differs from qualification report")
    return expected


def url_origin(value: object, label: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise PrivatePlanError(f"{label} origin differs")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password \
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise PrivatePlanError(f"{label} origin differs")
    origin = f"https://{parsed.hostname.lower()}"
    if parsed.port is not None:
        origin += f":{parsed.port}"
    return origin, parsed.hostname.lower()


def effective_contract(state: noictl.ConfigState) -> tuple[str, str, str, str, str]:
    config = state.effective
    hydro = config.get("hydro") or {}
    frontend = config.get("frontend_proxy") or {}
    orchestrator = config.get("orchestrator") or {}
    oj_origin, oj_hostname = url_origin(hydro.get("public_base_url"), "OJ")
    exam_origin, exam_domain = url_origin(orchestrator.get("public_base_url"), "exam")
    hydro_domain_id = hydro.get("domain_id")
    if not isinstance(hydro_domain_id, str) or not hydro_domain_id \
            or hydro_domain_id != hydro_domain_id.strip():
        raise PrivatePlanError("Hydro domain ID differs")
    if frontend.get("provider") != "caddy" \
            or str(frontend.get("domain", "")).lower() != exam_domain \
            or frontend.get("orchestrator_upstream") != "http://127.0.0.1:8600" \
            or oj_hostname == exam_domain:
        raise PrivatePlanError("closed frontend configuration differs")
    return oj_origin, exam_origin, oj_hostname, hydro_domain_id, exam_domain


def desired_definition(baseline: dict, plan_id: str, release: str,
                       image_id: str) -> dict:
    immutable = baseline["container"]["immutable_identity"]
    config = json.loads(json.dumps(immutable.get("config")))
    host = json.loads(json.dumps(immutable.get("host_config")))
    if not isinstance(config, dict) or not isinstance(host, dict):
        raise PrivatePlanError("controller backup definition differs")
    labels = config.get("Labels") or {}
    if not isinstance(labels, dict):
        raise PrivatePlanError("controller labels differ")
    labels = dict(labels)
    labels[LABEL_PLAN] = plan_id
    labels[LABEL_RELEASE] = release
    config["Labels"] = labels
    config["Image"] = image_id
    # Docker inspect can report an unset OomKillDisable as null, while the
    # create/inspect round trip canonicalizes the same behavior to false.
    # Freeze the canonical representation so exact post-create comparison is
    # stable without weakening any other HostConfig field.
    if host.get("OomKillDisable") is None:
        host["OomKillDisable"] = False
    if host.get("NetworkMode") != "host" \
            or (host.get("RestartPolicy") or {}).get("Name") != "unless-stopped" \
            or host.get("Privileged") is not False:
        raise PrivatePlanError("controller safety baseline differs")
    result = {"schema_version": 1, "plan_id": plan_id,
              "source_release": release, "image_id": image_id,
              "config": config, "host_config": host}
    if b"/var/run/docker.sock" in canonical(result) or b"/run/docker.sock" in canonical(result):
        raise PrivatePlanError("controller definition mounts Docker control socket")
    return result


def live_inputs_match_backup(args: argparse.Namespace, backup: Path) -> None:
    for live, name in ((args.project_config, "orchestrator-config.yaml"),
                       (args.project_env, "orchestrator.env"),
                       (args.caddyfile, "Caddyfile"),
                       (args.snippet, "caddy-exam.conf")):
        current, _ = safe_file(live, maximum=32 * 1024 * 1024)
        saved, _ = safe_file(backup / name, maximum=32 * 1024 * 1024)
        if current != saved:
            raise PrivatePlanError(f"live input changed after backup: {name}")


def build(args: argparse.Namespace) -> dict:
    if not HEX64.fullmatch(args.plan_id) or not HEX64.fullmatch(args.expected_manifest_sha256) \
            or not HEX64.fullmatch(args.backup_manifest_sha256):
        raise PrivatePlanError("private upgrade identity is invalid")
    scope = "qualification-lab" if args.qualification_lab else "production"
    for name in ("candidate", "backup_directory", "output_directory", "install_root",
                 "project_config", "project_env", "database", "caddyfile", "snippet",
                 "python_bin", "bash_bin", "pm2_bin", "node_bin", "docker_socket"):
        setattr(args, name, Path(os.path.abspath(getattr(args, name))))
    for name in ("python_bin", "bash_bin", "pm2_bin", "node_bin"):
        setattr(args, name, trusted_executable(getattr(args, name)))
    args.docker_socket = safe_docker_socket(args.docker_socket)
    for name in ("project_config", "project_env", "database", "caddyfile", "snippet"):
        path = getattr(args, name)
        safe_ancestors(path, f"private upgrade {name}")
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) \
                or metadata.st_nlink != 1 \
                or (platform.system().lower() == "linux" and metadata.st_uid != 0):
            raise PrivatePlanError(f"private upgrade {name} metadata differs")
    candidate = Path(os.path.abspath(args.candidate)).resolve(strict=True)
    manifest, _, verified = candidate_identity(
        candidate, args.expected_manifest_sha256,
        require_production=not args.qualification_lab,
    )
    backup, backup_manifest, baseline = load_backup(
        args.backup_directory, args.plan_id, args.backup_manifest_sha256
    )
    if backup_manifest["source"] != {
        "revision": verified["revision"], "manifest_sha256": verified["manifest_sha256"]
    }:
        raise PrivatePlanError("backup source differs from candidate")
    live_inputs_match_backup(args, backup)

    environment = runtime_environment(baseline)
    public_id, state = public_plan_identity(
        backup / "orchestrator-config.yaml", environment, verified, scope
    )
    if public_id != args.plan_id:
        raise PrivatePlanError("public install plan identity differs")
    oj_origin, exam_origin, hydro_domain, _hydro_domain_id, frontend_domain = effective_contract(state)

    image_id = qualification_image(
        candidate, manifest, args.controller_image_id, not args.qualification_lab
    )
    docker = Docker(args.docker_socket)
    docker.image(image_id)
    current = docker.inspect("noi-orchestrator")
    if not inspect_matches(current, baseline) or not (current.get("State") or {}).get("Running"):
        raise PrivatePlanError("live controller changed after backup")

    staged_source = source_plan(
        candidate, args.expected_manifest_sha256, args.install_root,
        qualification_lab=args.qualification_lab, owner_plan_id=args.plan_id,
    )
    final_output, output = private_staging(args.output_directory, args.plan_id)
    final_transaction = final_output / "transaction"
    transaction = output / "transaction"
    transaction.mkdir(mode=0o700)
    if platform.system().lower() == "linux":
        os.chown(transaction, 0, 0)
    os.chmod(transaction, 0o700)
    fsync_directory(output)

    desired_config = output / "desired-config.yaml"
    desired_env = output / "desired.env"
    exact_copy(backup / "orchestrator-config.yaml", desired_config)
    exact_copy(backup / "orchestrator.env", desired_env)
    desired_path = output / "desired-controller-definition.json"
    atomic(desired_path, canonical(desired_definition(
        baseline, args.plan_id, staged_source["release_name"], image_id
    )))
    contract_path = output / "post-install-contract.json"
    contract = {
        "schema_version": 1, "plan_id": args.plan_id,
        "source_release": staged_source["release_name"],
        "controller_image_id": image_id, "oj_origin": oj_origin,
        "exam_origin": exam_origin,
        "source_pointer": str(args.install_root / "current-source"),
        "caddyfile": str(args.caddyfile), "snippet": str(args.snippet),
        "project_config": str(args.project_config), "project_env": str(args.project_env),
        "database": str(args.database), "pm2_bin": str(args.pm2_bin),
        "docker_socket": str(args.docker_socket),
    }
    atomic(contract_path, canonical(contract))

    private_artifact_sha256 = {
        "expected_contract": hashlib.sha256(safe_private_file(contract_path, "post-install contract")[0]).hexdigest(),
        "desired_controller_definition": hashlib.sha256(safe_private_file(desired_path, "desired controller definition")[0]).hexdigest(),
        "desired_config": hashlib.sha256(safe_private_file(desired_config, "desired config")[0]).hexdigest(),
        "desired_env": hashlib.sha256(safe_private_file(desired_env, "desired env")[0]).hexdigest(),
    }

    plan_path = output / "private-upgrade-plan.json"
    final_plan_path = final_output / plan_path.name
    final_contract_path = final_output / contract_path.name
    final_desired_path = final_output / desired_path.name
    final_desired_config = final_output / desired_config.name
    final_desired_env = final_output / desired_env.name
    plan = {
        "schema_version": 1, "operation": "upgrade", "plan_id": args.plan_id,
        "scope": scope, "source_plan_id": staged_source["plan_id"],
        "source_release": staged_source["release_name"], "candidate": str(candidate),
        "candidate_manifest_sha256": args.expected_manifest_sha256,
        "backup_directory": str(backup),
        "backup_manifest_sha256": args.backup_manifest_sha256,
        "transaction_directory": str(final_transaction), "install_root": str(args.install_root),
        "expected_contract": str(final_contract_path),
        "private_artifact_sha256": private_artifact_sha256,
        "desired_controller_definition": str(final_desired_path),
        "desired_config": str(final_desired_config), "desired_env": str(final_desired_env),
        "project_config": str(args.project_config), "project_env": str(args.project_env),
        "database": str(args.database), "caddyfile": str(args.caddyfile),
        "snippet": str(args.snippet), "hydro_domain": hydro_domain,
        "frontend_domain": frontend_domain,
        "orchestrator_upstream": "http://127.0.0.1:8600",
        "executables": {"python": str(args.python_bin), "bash": str(args.bash_bin),
            "pm2": str(args.pm2_bin), "node": str(args.node_bin),
            "docker_socket": str(args.docker_socket)},
    }
    atomic(plan_path, canonical(plan))
    plan_raw, _ = safe_private_file(plan_path, "private upgrade plan", maximum=2 * 1024 * 1024)
    plan_sha = hashlib.sha256(plan_raw).hexdigest()
    staged_verified = verify_private_plan(plan_path, plan_sha)
    verify_desired_definition(desired_path, args.plan_id, staged_source["release_name"])
    staged_verified = dict(staged_verified)
    staged_verified.update({
        "transaction_directory": str(transaction),
        "expected_contract": str(contract_path),
        "desired_controller_definition": str(desired_path),
        "desired_config": str(desired_config), "desired_env": str(desired_env),
    })
    verify_bindings(staged_verified)
    output = publish_directory(output, final_output)
    verified_plan = verify_private_plan(final_plan_path, plan_sha)
    verify_bindings(verified_plan)
    verify_desired_definition(final_desired_path, args.plan_id, staged_source["release_name"])
    return {"status": "planned", "operation": "upgrade", "plan_id": args.plan_id,
            "private_plan": str(final_plan_path),
            "private_plan_sha256": plan_sha,
            "source_plan_id": staged_source["plan_id"],
            "source_release": staged_source["release_name"],
            "service_mutations": 0}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--plan-id", required=True)
    value.add_argument("--candidate", required=True, type=Path)
    value.add_argument("--expected-manifest-sha256", required=True)
    value.add_argument("--controller-image-id", required=True)
    value.add_argument("--backup-directory", required=True, type=Path)
    value.add_argument("--backup-manifest-sha256", required=True)
    value.add_argument("--output-directory", required=True, type=Path)
    value.add_argument("--install-root", required=True, type=Path)
    value.add_argument("--project-config", required=True, type=Path)
    value.add_argument("--project-env", required=True, type=Path)
    value.add_argument("--database", required=True, type=Path)
    value.add_argument("--caddyfile", required=True, type=Path)
    value.add_argument("--snippet", required=True, type=Path)
    value.add_argument("--python-bin", required=True, type=Path)
    value.add_argument("--bash-bin", required=True, type=Path)
    value.add_argument("--pm2-bin", required=True, type=Path)
    value.add_argument("--node-bin", required=True, type=Path)
    value.add_argument("--docker-socket", default=Path("/var/run/docker.sock"), type=Path)
    value.add_argument("--qualification-lab", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise PrivatePlanError("private upgrade planning requires Linux root")
        trusted_self()
        print(json.dumps(build(args), sort_keys=True))
        return 0
    except (PrivatePlanError, OSError, ValueError, UnicodeDecodeError,
            json.JSONDecodeError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
