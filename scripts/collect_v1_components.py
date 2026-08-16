#!/usr/bin/env python3
"""Collect one role-local immutable component identity without secrets."""
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


IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
HEX40 = re.compile(r"^[a-f0-9]{40}$")
CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PLUGIN_FILES = (
    "index.js",
    "package.json",
)


class ComponentError(RuntimeError):
    pass


def require_private_output_parent(path: Path) -> Path:
    parent = Path(os.path.abspath(path)).parent
    if not parent.is_dir() or parent.is_symlink():
        raise ComponentError("component output parent must be a real directory")
    resolved = parent.resolve()
    if resolved != parent:
        raise ComponentError("component output parent must be canonical")
    current = Path(resolved.anchor)
    for part in resolved.parts[1:]:
        current = current / part
        info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or (
                platform.system().lower() == "linux"
                and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022)
            )
        ):
            raise ComponentError("component output parent has an unsafe ancestor")
    if stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise ComponentError("component output parent must be mode 0700 or stricter")
    return parent


def run(command: list[str], label: str) -> str:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ComponentError(f"{label} could not complete") from exc
    value = result.stdout.strip()
    if result.returncode or not value or len(value) > 4096 or any(ord(ch) < 32 for ch in value):
        raise ComponentError(f"{label} returned an invalid value")
    return value


def require_trusted_executable(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ComponentError(f"{label} path must be absolute")
    requested = Path(os.path.abspath(path))
    resolved = requested.resolve(strict=True)
    if requested != resolved:
        raise ComponentError(f"{label} executable path must be canonical")
    info = resolved.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise ComponentError(f"{label} executable metadata is unsafe")
    current = Path(resolved.anchor)
    for part in resolved.parts[1:-1]:
        current = current / part
        ancestor = current.lstat()
        if (
            not stat.S_ISDIR(ancestor.st_mode)
            or stat.S_ISLNK(ancestor.st_mode)
            or ancestor.st_uid != 0
            or stat.S_IMODE(ancestor.st_mode) & 0o022
        ):
            raise ComponentError(f"{label} executable ancestor is unsafe")
    return resolved


def docker_container_image(
    docker_bin: Path, name: str, label: str, *, require_running: bool
) -> str:
    binary = str(docker_bin)
    state = run([binary, "inspect", "--format", "{{.State.Running}}", name], f"{label} state")
    if require_running and state != "true":
        raise ComponentError(f"{label} must be running")
    value = run([binary, "inspect", "--format", "{{.Image}}", name], f"{label} image")
    if not IMAGE_ID.fullmatch(value):
        raise ComponentError(f"{label} image ID is invalid")
    resolved = run([binary, "image", "inspect", value, "--format", "{{.Id}}"], f"{label} image resolution")
    if resolved != value:
        raise ComponentError(f"{label} image identity changed")
    return value


def image_label(docker_bin: Path, image_id: str, name: str, label: str) -> str:
    return run(
        [str(docker_bin), "image", "inspect", image_id, "--format", f"{{{{index .Config.Labels {json.dumps(name)}}}}}"],
        label,
    )


def read_plugin_file(root: Path, relative: str) -> bytes:
    path = root / Path(relative)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ComponentError(f"cannot open plugin file safely: {relative}") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (
                platform.system().lower() == "linux"
                and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022)
            )
            or info.st_size > 4 * 1024 * 1024
        ):
            raise ComponentError(f"plugin file metadata is unsafe: {relative}")
        raw = b""
        while len(raw) <= 4 * 1024 * 1024:
            block = os.read(descriptor, 65536)
            if not block:
                break
            raw += block
        if len(raw) != info.st_size:
            raise ComponentError(f"plugin file changed while reading: {relative}")
        return raw
    finally:
        os.close(descriptor)


def plugin_digest(root: Path) -> str:
    requested = Path(os.path.abspath(root))
    if requested.is_symlink() or not requested.is_dir():
        raise ComponentError("Hydro plugin root must be a real directory")
    root = requested.resolve()
    if root != requested:
        raise ComponentError("Hydro plugin root must be canonical")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or (
                platform.system().lower() == "linux"
                and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022)
            )
        ):
            raise ComponentError("Hydro plugin root has an unsafe ancestor")
    observed = sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    )
    if observed != sorted(PLUGIN_FILES):
        raise ComponentError("Hydro plugin file set differs")
    digest = hashlib.sha256()
    for relative in sorted(PLUGIN_FILES):
        raw = read_plugin_file(root, relative)
        name = relative.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def collect_control(docker_bin: Path, container: str) -> dict:
    return {
        "role": "control",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "orchestrator_image_digest": docker_container_image(
            docker_bin, container, "orchestrator container", require_running=True
        ),
    }


def collect_desktop(docker_bin: Path, container: str) -> dict:
    image = docker_container_image(
        docker_bin, container, "desktop seat", require_running=True
    )
    contract = image_label(
        docker_bin, image, "org.noi.desktop.contract", "desktop contract"
    )
    revision = image_label(
        docker_bin, image, "org.opencontainers.image.revision", "desktop revision"
    )
    if contract != "finalizer-status-v1" or not HEX40.fullmatch(revision):
        raise ComponentError("desktop image labels differ from V1")
    return {
        "role": "desktop",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "desktop_contract": contract,
        "desktop_image_id": image,
        "desktop_source_revision": revision,
    }


def collect_oj(plugin_root: Path) -> dict:
    return {
        "role": "oj",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "hydro_plugin_sha256": plugin_digest(plugin_root),
    }


def validate_role_component(value: dict, expected_role: str) -> dict:
    shapes = {
        "control": {"role", "observed_at", "orchestrator_image_digest"},
        "desktop": {"role", "observed_at", "desktop_contract", "desktop_image_id", "desktop_source_revision"},
        "oj": {"role", "observed_at", "hydro_plugin_sha256"},
    }
    if not isinstance(value, dict) or set(value) != shapes[expected_role] or value.get("role") != expected_role:
        raise ComponentError(f"{expected_role} component fact shape differs")
    if expected_role == "control" and not IMAGE_ID.fullmatch(str(value["orchestrator_image_digest"])):
        raise ComponentError("control image digest is invalid")
    if expected_role == "desktop" and (
        value["desktop_contract"] != "finalizer-status-v1"
        or not IMAGE_ID.fullmatch(str(value["desktop_image_id"]))
        or not HEX40.fullmatch(str(value["desktop_source_revision"]))
    ):
        raise ComponentError("desktop component identity is invalid")
    if expected_role == "oj" and not re.fullmatch(r"[a-f0-9]{64}", str(value["hydro_plugin_sha256"])):
        raise ComponentError("Hydro plugin digest is invalid")
    observed_at = value["observed_at"]
    if not isinstance(observed_at, str) or not observed_at.endswith("Z"):
        raise ComponentError("component observed_at is invalid")
    try:
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ComponentError("component observed_at is invalid") from exc
    return value


def atomic_json(path: Path, value: dict) -> str:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        raise ComponentError("component output must not already exist")
    require_private_output_parent(requested)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".v1-components-", dir=requested.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, requested)
        directory = os.open(requested.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("control", "desktop", "oj"), required=True)
    parser.add_argument("--container")
    parser.add_argument("--docker-bin", type=Path)
    parser.add_argument("--plugin-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux" or os.geteuid() != 0:
            raise ComponentError("component collection requires Linux root")
        if args.role in {"control", "desktop"}:
            if not args.container or args.plugin_root is not None or args.docker_bin is None:
                raise ComponentError(
                    "control/desktop roles require --container and --docker-bin"
                )
            if not CONTAINER_NAME.fullmatch(args.container):
                raise ComponentError("container name is invalid")
            docker_bin = require_trusted_executable(args.docker_bin, "Docker")
            value = (
                collect_control(docker_bin, args.container)
                if args.role == "control"
                else collect_desktop(docker_bin, args.container)
            )
        else:
            if args.plugin_root is None or args.container is not None or args.docker_bin is not None:
                raise ComponentError("OJ role requires only --plugin-root")
            value = collect_oj(args.plugin_root)
        validate_role_component(value, args.role)
        digest = atomic_json(args.output, value)
        print(json.dumps({"components_sha256": digest, "role": args.role, "status": "passed"}, sort_keys=True))
        return 0
    except (ComponentError, OSError) as exc:
        print(f"NO_GO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
