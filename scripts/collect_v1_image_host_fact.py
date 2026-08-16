#!/usr/bin/env python3
"""Collect one read-only Linux host fact for cross-machine image qualification."""
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


ROOT = Path(__file__).resolve().parents[1]
HEX40 = re.compile(r"^[a-f0-9]{40}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
SESSION = re.compile(r"^[a-f0-9]{64}$")
SOURCE_TARGET = re.compile(r"^image-releases/[A-Za-z0-9TZ-]+$")
PHASES = {"export", "imported", "promoted", "rolled_back", "repromoted", "restored"}


class FactError(RuntimeError):
    pass


def read_regular(path: Path, limit: int, label: str) -> bytes:
    if not path.is_absolute():
        path = path.resolve()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FactError(f"cannot open {label} as a no-follow file: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise FactError(f"{label} must be a single-link regular file")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise FactError(f"{label} exceeds its size limit")
    finally:
        os.close(descriptor)


def json_regular(path: Path, limit: int, label: str) -> tuple[dict, bytes]:
    raw = read_regular(path, limit, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactError(f"{label} is not strict UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise FactError(f"{label} root must be an object")
    return value, raw


def run(command: list[str], label: str, *, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactError(f"{label} could not complete: {exc}") from exc
    value = result.stdout.strip()
    if result.returncode or not value or len(value) > 1024 or any(ord(ch) < 32 for ch in value):
        raise FactError(f"{label} returned an invalid result")
    return value


def run_allow_empty(command: list[str], label: str, *, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactError(f"{label} could not complete: {exc}") from exc
    value = result.stdout.strip()
    if result.returncode or len(value) > 1024 * 1024:
        raise FactError(f"{label} returned an invalid result")
    return value


def regular_digest(path: Path, expected_size: int, label: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path.resolve(), flags)
    except OSError as exc:
        raise FactError(f"cannot open {label} as a no-follow file: {exc}") from exc
    digest = hashlib.sha256()
    observed = 0
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise FactError(f"{label} must be a single-link regular file")
        if info.st_size != expected_size:
            raise FactError(f"{label} size differs")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            observed += len(chunk)
    finally:
        os.close(descriptor)
    if observed != expected_size:
        raise FactError(f"{label} changed while hashing")
    return digest.hexdigest()


def git(*arguments: str) -> str:
    return run(["git", *arguments], f"git {' '.join(arguments)}")


def git_status_porcelain() -> str:
    """Return tracked worktree changes; a clean checkout is valid empty output."""
    arguments = ["status", "--porcelain=v1", "--untracked-files=no"]
    return run_allow_empty(["git", *arguments], f"git {' '.join(arguments)}")


def docker_image_id(reference: str) -> str:
    value = run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        f"docker image inspect {reference}",
    )
    if not IMAGE_ID.fullmatch(value):
        raise FactError(f"Docker returned an invalid image ID for {reference}")
    return value


def docker_image_labels(reference: str) -> dict[str, str]:
    raw = run(
        ["docker", "image", "inspect", reference, "--format", "{{json .Config.Labels}}"],
        f"docker image labels {reference}",
    )
    try:
        labels = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FactError("Docker returned invalid image labels") from exc
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in labels.items()
    ):
        raise FactError("Docker returned invalid image labels")
    return labels


def acquire_deployment_lock() -> int:
    try:
        import fcntl
    except ImportError as exc:
        raise FactError("shared deployment locking requires Linux fcntl") from exc
    path = Path("/var/lock/noi-official-image-deploy.lock")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FactError(f"cannot open the shared deployment lock safely: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) not in {0o600, 0o644}
        ):
            raise FactError("shared deployment lock metadata is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FactError("an image deployment, rollback, or fact collection is running") from exc
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def require_root_managed_directory(path: Path, label: str) -> None:
    path = Path(os.path.abspath(path))
    if path != Path(os.path.realpath(path)) or not path.is_dir() or path.is_symlink():
        raise FactError(f"{label} must be a real canonical directory")
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


def exact_bundle(bundle_dir: Path, release_path: Path) -> tuple[dict[str, object], dict[str, str]]:
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise FactError("bundle directory must be a real directory")
    if release_path.is_symlink():
        raise FactError("release manifest must not be a symlink")
    manifest, manifest_raw = json_regular(bundle_dir / "manifest.json", 1024 * 1024, "bundle manifest")
    release, release_raw = json_regular(release_path, 1024 * 1024, "release manifest")
    if manifest.get("$schema") != "local-image-bundle-manifest.schema.json" or manifest.get("schema_version") != 1:
        raise FactError("unsupported bundle manifest")
    if release.get("$schema") != "release-manifest.schema.json" or release.get("schema_version") != 1:
        raise FactError("unsupported release manifest")
    image = manifest.get("image")
    archive = manifest.get("archive")
    desktop = ((release.get("components") or {}).get("desktop") if isinstance(release.get("components"), dict) else None)
    release_row = release.get("release")
    if not all(isinstance(value, dict) for value in (image, archive, desktop, release_row)):
        raise FactError("bundle or release manifest shape differs")
    labels = image.get("labels")
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in labels.items()
    ):
        raise FactError("bundle image labels are missing")
    required = {
        "image_id": image.get("id"),
        "image_tag": image.get("tag"),
        "source_revision": image.get("source_revision"),
        "contract": labels.get("org.noi.desktop.contract"),
        "iso_sha256": labels.get("org.noi.iso.sha256"),
        "archive_sha256": archive.get("sha256"),
    }
    if not IMAGE_ID.fullmatch(str(required["image_id"])):
        raise FactError("bundle image ID is invalid")
    if not isinstance(required["image_tag"], str) or ":" not in required["image_tag"] or required["image_tag"].endswith(":latest"):
        raise FactError("bundle image tag is invalid")
    if not HEX40.fullmatch(str(required["source_revision"])):
        raise FactError("bundle source revision is invalid")
    if required["contract"] != "finalizer-status-v1":
        raise FactError("bundle desktop contract differs")
    if not HEX64.fullmatch(str(required["iso_sha256"])) or not HEX64.fullmatch(str(required["archive_sha256"])):
        raise FactError("bundle ISO or archive digest is invalid")
    if labels.get("org.opencontainers.image.revision") != required["source_revision"]:
        raise FactError("bundle OCI revision differs")
    archive_name = archive.get("file")
    if not isinstance(archive_name, str) or Path(archive_name).name != archive_name:
        raise FactError("bundle archive basename is invalid")
    expected_entries = {
        archive_name,
        "manifest.json",
        "local-image-bundle-manifest.schema.json",
        "import-local-image-bundle.sh",
        "SHA256SUMS",
    }
    observed_entries = {path.name for path in bundle_dir.iterdir()}
    if observed_entries != expected_entries:
        raise FactError("bundle directory entries differ")
    archive_size = archive.get("size_bytes")
    if not isinstance(archive_size, int) or archive_size < 1:
        raise FactError("bundle archive size is invalid")
    if regular_digest(bundle_dir / archive_name, archive_size, "bundle archive") != required["archive_sha256"]:
        raise FactError("bundle archive bytes differ")
    checksums_raw = read_regular(bundle_dir / "SHA256SUMS", 64 * 1024, "bundle checksums")
    try:
        checksum_lines = checksums_raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise FactError("bundle checksums are not ASCII") from exc
    checksum_rows = {}
    for line in checksum_lines:
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9._-]+)", line)
        if match is None or match.group(2) in checksum_rows:
            raise FactError("bundle checksums have an invalid or duplicate row")
        checksum_rows[match.group(2)] = match.group(1)
    expected_checksum_names = expected_entries - {"SHA256SUMS"}
    if set(checksum_rows) != expected_checksum_names:
        raise FactError("bundle checksums do not cover the exact bundle payload")
    checksum_limits = {
        archive_name: archive_size,
        "manifest.json": 1024 * 1024,
        "local-image-bundle-manifest.schema.json": 4 * 1024 * 1024,
        "import-local-image-bundle.sh": 4 * 1024 * 1024,
    }
    for name, expected_digest in checksum_rows.items():
        if name == archive_name:
            actual_digest = required["archive_sha256"]
        else:
            actual_digest = hashlib.sha256(
                read_regular(bundle_dir / name, checksum_limits[name], f"bundle file {name}")
            ).hexdigest()
        if actual_digest != expected_digest:
            raise FactError(f"bundle checksum differs for {name}")
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    checksums_sha = hashlib.sha256(checksums_raw).hexdigest()
    release_git_revision = release_row.get("git_revision")
    comparisons = {
        "bundle_manifest_sha256": manifest_sha,
        "bundle_checksums_sha256": checksums_sha,
        "image_id": required["image_id"],
        "image_tag": required["image_tag"],
        "source_revision": required["source_revision"],
        "contract": required["contract"],
        "iso_sha256": required["iso_sha256"],
    }
    for key, actual in comparisons.items():
        release_key = key
        if desktop.get(release_key) != actual:
            raise FactError(f"release desktop {release_key} differs from bundle")
    if release_git_revision != required["source_revision"]:
        raise FactError("release Git revision differs from bundle")
    return ({
        **required,
        "bundle_manifest_sha256": manifest_sha,
        "bundle_checksums_sha256": checksums_sha,
        "release_manifest_sha256": hashlib.sha256(release_raw).hexdigest(),
    }, labels)


def parse_promotion(app_root: Path) -> dict[str, object]:
    pending = app_root / "image-promotion.pending"
    pending_exists = pending.exists() or pending.is_symlink()
    current_link = app_root / "current-image-source"
    if not current_link.is_symlink():
        raise FactError("current-image-source is not a symlink")
    target = os.readlink(current_link)
    if not SOURCE_TARGET.fullmatch(target):
        raise FactError("current source target is unsafe")
    metadata_path = app_root / target / "promotion.env"
    raw = read_regular(metadata_path, 64 * 1024, "promotion metadata")
    values = {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FactError("promotion metadata is not UTF-8") from exc
    for line in text.splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise FactError("promotion metadata has a duplicate key")
        values[key] = value
    promoted = values.get("PROMOTED_IMAGE_ID", "")
    source_revision = values.get("SOURCE_REVISION", "")
    recorded_target = values.get("SOURCE_TARGET", "")
    if not IMAGE_ID.fullmatch(promoted) or recorded_target != target:
        raise FactError("promotion image/source pair is invalid")
    if source_revision and not HEX40.fullmatch(source_revision):
        raise FactError("promotion source revision is invalid")
    rollback_image = values.get("ROLLBACK_IMAGE_ID") or None
    rollback_source = values.get("ROLLBACK_SOURCE_TARGET") or None
    if rollback_image is not None and not IMAGE_ID.fullmatch(rollback_image):
        raise FactError("rollback image ID is invalid")
    if rollback_source is not None and not SOURCE_TARGET.fullmatch(rollback_source):
        raise FactError("rollback source target is invalid")
    formal = docker_image_id("noi-linux-official:2.0")
    if formal != promoted:
        raise FactError("formal image and source metadata are inconsistent")
    return {
        "current_promoted_image_id": promoted,
        "current_rollback_image_id": rollback_image,
        "current_rollback_source_target": rollback_source,
        "current_source_revision": source_revision or None,
        "current_source_target": target,
        "formal_image_id": formal,
        "pending_transaction": pending_exists,
    }


def atomic_json(path: Path, document: dict[str, object]) -> str:
    requested = Path(os.path.abspath(path))
    if os.path.lexists(requested):
        raise FactError("fact output must not already exist")
    requested.parent.mkdir(parents=True, exist_ok=True)
    path = requested.parent.resolve() / requested.name
    raw = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".v1-image-fact-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
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
    parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if platform.system().lower() != "linux":
            raise FactError("image qualification facts require Linux")
        if os.geteuid() != 0:
            raise FactError("image qualification facts must be collected as root")
        if not SESSION.fullmatch(args.session_id):
            raise FactError("session ID must be 64 lowercase hexadecimal characters")
        promotion_phase = args.phase in {"imported", "promoted", "rolled_back", "repromoted", "restored"}
        if promotion_phase and args.app_root is None:
            raise FactError("--app-root is required for promotion phases")
        if not promotion_phase and args.app_root is not None:
            raise FactError("--app-root is not allowed for export phase")
        lock_descriptor = acquire_deployment_lock() if promotion_phase else None
        require_root_managed_directory(ROOT, "collector checkout")
        revision = git("rev-parse", "HEAD")
        tree = git("rev-parse", "HEAD^{tree}")
        if git_status_porcelain():
            raise FactError("tracked Git worktree must be clean")
        bundle_path = Path(os.path.abspath(args.bundle_dir))
        release_path = Path(os.path.abspath(args.release_manifest))
        require_root_managed_directory(bundle_path, "bundle directory")
        require_root_managed_directory(release_path.parent, "release manifest directory")
        bundle, bundle_labels = exact_bundle(bundle_path, release_path)
        if revision != bundle["source_revision"] or not HEX40.fullmatch(tree):
            raise FactError("collector checkout differs from bundle revision")
        machine_id = read_regular(Path("/etc/machine-id"), 4096, "machine ID").strip()
        if not machine_id:
            raise FactError("machine ID is empty")
        host_id = hashlib.sha256(args.session_id.encode("ascii") + b":" + machine_id).hexdigest()
        candidate_id = docker_image_id(str(bundle["image_tag"]))
        if candidate_id != bundle["image_id"]:
            raise FactError("candidate tag differs from bundle image ID")
        candidate_labels = docker_image_labels(str(bundle["image_tag"]))
        if candidate_labels != bundle_labels:
            raise FactError("candidate image labels differ from the bundle")
        seat_ids = run_allow_empty(
            ["docker", "ps", "-q", "--filter", "label=noi.contest"],
            "running contest seat query",
        ).splitlines()
        if any(seat_ids):
            raise FactError("contest seat containers are running")
        state = {
            "candidate_tag_image_id": candidate_id,
            "current_promoted_image_id": None,
            "current_rollback_image_id": None,
            "current_rollback_source_target": None,
            "current_source_revision": None,
            "current_source_target": None,
            "formal_image_id": None,
            "pending_transaction": False,
            "running_contest_seats": 0,
        }
        if promotion_phase:
            app_root = Path(os.path.abspath(args.app_root))
            require_root_managed_directory(app_root, "app root")
            state.update(parse_promotion(app_root))
        if state["pending_transaction"]:
            raise FactError("image promotion transaction is pending")
        document = {
            "$schema": "v1-image-host-fact.schema.json",
            "schema_version": 1,
            "phase": args.phase,
            "session_id": args.session_id,
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "host": {
                "anonymous_id": host_id,
                "architecture": platform.machine(),
                "docker_server": run(["docker", "version", "--format", "{{.Server.Version}}"], "Docker server version"),
                "kernel": platform.release(),
            },
            "source": {"revision": revision, "tree": tree},
            "bundle": bundle,
            "state": state,
        }
        output_path = Path(os.path.abspath(args.output))
        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require_root_managed_directory(output_path.parent, "evidence output directory")
        digest = atomic_json(output_path, document)
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        print(json.dumps({"fact_sha256": digest, "phase": args.phase, "status": "passed"}, sort_keys=True))
        return 0
    except FactError as exc:
        print(f"IMAGE_FACT_FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
