#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
    cat <<'EOF'
Usage:
  import-local-image-bundle.sh --bundle-dir PATH --release-manifest PATH
      [--replace-existing]

By default an existing tag that points to a different image ID is never changed.
The importer must come from the trusted public release checkout, not the bundle.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

bundle_dir=''
release_manifest=''
replace_existing=0

while (($#)); do
    case "$1" in
        --bundle-dir)
            (($# >= 2)) || die '--bundle-dir requires a value'
            bundle_dir="$2"
            shift 2
            ;;
        --release-manifest)
            (($# >= 2)) || die '--release-manifest requires a value'
            release_manifest="$2"
            shift 2
            ;;
        --replace-existing)
            replace_existing=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

[[ -n "${bundle_dir}" ]] || die '--bundle-dir is required'
[[ -n "${release_manifest}" ]] || die '--release-manifest is required'
bundle_dir="${bundle_dir%/}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
archive_identity_verifier="${script_dir}/verify_docker_archive_identity.py"
[[ -f "${archive_identity_verifier}" ]] \
    || die "trusted archive identity verifier not found: ${archive_identity_verifier}"

for command_name in docker python3 tar sha256sum stat mktemp awk grep sed rm; do
    need_cmd "${command_name}"
done

tmp_dir="$(mktemp -d)"
restore_needed=0
remove_loaded_tag=0
previous_id=''
image_tag=''
cleanup() {
    status=$?
    trap - EXIT
    if ((status != 0 && restore_needed == 1)) && [[ -n "${previous_id}" && -n "${image_tag}" ]]; then
        printf 'Import failed; restoring %s to %s...\n' "${image_tag}" "${previous_id}" >&2
        if ! docker image tag "${previous_id}" "${image_tag}"; then
            printf 'error: automatic tag restore failed; restore it manually\n' >&2
        fi
    elif ((status != 0 && remove_loaded_tag == 1)) && [[ -n "${image_tag}" ]]; then
        cleanup_inspect_error="${tmp_dir}/cleanup-tag-inspect.err"
        if docker image inspect "${image_tag}" >/dev/null 2>"${cleanup_inspect_error}"; then
            printf 'Import failed; removing newly loaded tag %s...\n' "${image_tag}" >&2
            if ! docker image rm "${image_tag}" >/dev/null 2>&1; then
                printf 'warning: could not remove the newly loaded tag; inspect it manually\n' >&2
            fi
        elif ! grep -Eq '^(Error response from daemon: |Error: )?No such image: ' \
            "${cleanup_inspect_error}"; then
            printf 'warning: Docker state is unavailable; %s may require manual cleanup\n' \
                "${image_tag}" >&2
        fi
    fi
    rm -rf -- "${tmp_dir}"
    exit "${status}"
}
trap cleanup EXIT

# Treat the delivered directory as hostile input. Open the public Release
# manifest and every bundle source without following links, require exactly the
# five exported physical files, then copy only externally bound bytes into a
# private directory. All subsequent validation and Docker input use this
# immutable-by-convention snapshot.
snapshot_dir="${tmp_dir}/bundle"
release_snapshot="${tmp_dir}/release-manifest.json"
snapshot_error="$({
python3 - "${bundle_dir}" "${release_manifest}" "${snapshot_dir}" \
    "${release_snapshot}" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys

source_dir, release_path, snapshot_dir, release_snapshot = sys.argv[1:]
required_payload = {
    "manifest.json",
    "local-image-bundle-manifest.schema.json",
    "import-local-image-bundle.sh",
}
name_pattern = re.compile(r"[A-Za-z0-9._-]+")
checksum_pattern = re.compile(r"([a-f0-9]{64})  ([A-Za-z0-9._-]+)")
digest_pattern = re.compile(r"[a-f0-9]{64}")

if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
    raise SystemExit("this importer requires O_NOFOLLOW and O_DIRECTORY support")

close_on_exec = getattr(os, "O_CLOEXEC", 0)
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | close_on_exec
file_flags = os.O_RDONLY | os.O_NOFOLLOW | close_on_exec


def open_directory_path(path: str) -> int:
    if not isinstance(path, str) or not path or "\0" in path:
        raise SystemExit("directory path must be a non-empty string")
    absolute = os.path.isabs(path)
    try:
        current = os.open("/" if absolute else ".", directory_flags)
    except OSError as exc:
        raise SystemExit(f"cannot open path root without following links: {exc}")
    try:
        for component in path.split(os.sep):
            if component in {"", "."}:
                continue
            if component == "..":
                raise SystemExit("parent-directory components are not allowed")
            try:
                next_fd = os.open(component, directory_flags, dir_fd=current)
            except OSError as exc:
                raise SystemExit(
                    f"cannot open directory component without following links: "
                    f"{component}: {exc}"
                )
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def open_path_regular(path: str) -> int:
    if not isinstance(path, str) or not path or "\0" in path:
        raise SystemExit("release manifest path must be a non-empty string")
    parent, basename = os.path.split(path)
    if basename in {"", ".", ".."}:
        raise SystemExit("release manifest path must name a file")
    parent_fd = open_directory_path(parent or ".")
    try:
        try:
            fd = os.open(basename, file_flags, dir_fd=parent_fd)
        except OSError as exc:
            raise SystemExit(
                f"release manifest must be a real, no-follow file: {exc}"
            )
    finally:
        os.close(parent_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise SystemExit("release manifest is not a regular file")
    return fd


def read_bounded_fd(fd: int, label: str, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise SystemExit(f"metadata is too large: {label}")


release_fd = open_path_regular(release_path)
try:
    release_bytes = read_bounded_fd(
        release_fd, "release-manifest.json", 1024 * 1024
    )
finally:
    os.close(release_fd)
try:
    release_document = json.loads(release_bytes.decode("utf-8"))
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"release manifest is not strict UTF-8 JSON: {exc}")
if not isinstance(release_document, dict):
    raise SystemExit("release manifest root must be an object")
if release_document.get("$schema") != "release-manifest.schema.json":
    raise SystemExit("unsupported release manifest schema reference")
if release_document.get("schema_version") != 1:
    raise SystemExit("unsupported release manifest schema_version")
components = release_document.get("components")
desktop = components.get("desktop") if isinstance(components, dict) else None
if not isinstance(desktop, dict):
    raise SystemExit("release manifest is missing components.desktop")
release_manifest_sha = desktop.get("bundle_manifest_sha256")
release_checksums_sha = desktop.get("bundle_checksums_sha256")
if not isinstance(release_manifest_sha, str) or not digest_pattern.fullmatch(
    release_manifest_sha
):
    raise SystemExit("release manifest has an invalid bundle_manifest_sha256")
if not isinstance(release_checksums_sha, str) or not digest_pattern.fullmatch(
    release_checksums_sha
):
    raise SystemExit("release manifest has an invalid bundle_checksums_sha256")

try:
    source_fd = open_directory_path(source_dir)
except SystemExit as exc:
    raise SystemExit(f"bundle directory must be a real, no-follow directory: {exc}")


def open_regular(name: str):
    if not name_pattern.fullmatch(name) or name in {".", ".."}:
        raise SystemExit(f"invalid bundle basename: {name!r}")
    try:
        fd = os.open(name, file_flags, dir_fd=source_fd)
    except OSError as exc:
        raise SystemExit(f"cannot open bundle file without following links: {name}: {exc}")
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise SystemExit(f"bundle member is not a regular file: {name}")
    return fd


def read_small(name: str, limit: int) -> bytes:
    fd = open_regular(name)
    try:
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise SystemExit(f"bundle metadata is too large: {name}")
    finally:
        os.close(fd)


manifest_bytes = read_small("manifest.json", 1024 * 1024)
try:
    manifest = json.loads(manifest_bytes.decode("utf-8"))
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"manifest is not strict UTF-8 JSON: {exc}")
archive = manifest.get("archive") if isinstance(manifest, dict) else None
archive_name = archive.get("file") if isinstance(archive, dict) else None
if not isinstance(archive_name, str) or not re.fullmatch(
    r"[A-Za-z0-9._-]+\.tar(?:\.zst)?", archive_name
):
    raise SystemExit("manifest has an invalid archive basename")

checksum_bytes = read_small("SHA256SUMS", 64 * 1024)
try:
    checksum_text = checksum_bytes.decode("ascii")
except UnicodeDecodeError as exc:
    raise SystemExit(f"SHA256SUMS must be ASCII: {exc}")
lines = checksum_text.splitlines()
if not lines or any(not line for line in lines):
    raise SystemExit("SHA256SUMS must contain only non-empty checksum lines")
entries = {}
for line in lines:
    match = checksum_pattern.fullmatch(line)
    if not match:
        raise SystemExit("SHA256SUMS contains a malformed or unsafe entry")
    digest, name = match.groups()
    if name in entries:
        raise SystemExit(f"SHA256SUMS contains a duplicate entry: {name}")
    entries[name] = digest

expected_payload = required_payload | {archive_name}
expected_physical = expected_payload | {"SHA256SUMS"}
try:
    initial_names = os.listdir(source_fd)
except OSError as exc:
    raise SystemExit(f"cannot enumerate bundle directory: {exc}")
if len(initial_names) != len(set(initial_names)) or set(initial_names) != expected_physical:
    missing = sorted(expected_physical - set(initial_names))
    extra = sorted(set(initial_names) - expected_physical)
    raise SystemExit(
        f"bundle physical entry set mismatch; missing={missing}, unexpected={extra}"
    )
if set(entries) != expected_payload:
    missing = sorted(expected_payload - set(entries))
    extra = sorted(set(entries) - expected_payload)
    raise SystemExit(
        f"SHA256SUMS entry set mismatch; missing={missing}, unexpected={extra}"
    )
manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
checksums_digest = hashlib.sha256(checksum_bytes).hexdigest()
if manifest_digest != release_manifest_sha:
    raise SystemExit("bundle manifest SHA256 does not match the public Release manifest")
if entries["manifest.json"] != release_manifest_sha:
    raise SystemExit("SHA256SUMS does not bind the public bundle manifest SHA256")
if checksums_digest != release_checksums_sha:
    raise SystemExit("SHA256SUMS SHA256 does not match the public Release manifest")

archive_size = archive.get("size_bytes")
if isinstance(archive_size, bool) or not isinstance(archive_size, int) or archive_size < 1:
    raise SystemExit("manifest has an invalid archive size")


def copy_verified(name: str, expected_digest: str, limit: int, exact_size=None):
    source_file_fd = open_regular(name)
    destination = os.path.join(snapshot_dir, name)
    destination_fd = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | close_on_exec, 0o600
    )
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(source_file_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise SystemExit(f"bundle member is larger than allowed: {name}")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
    finally:
        os.close(source_file_fd)
        os.close(destination_fd)
    if exact_size is not None and total != exact_size:
        raise SystemExit(f"bundle member size changed while snapshotting: {name}")
    if digest.hexdigest() != expected_digest:
        raise SystemExit(f"bundle checksum mismatch while snapshotting: {name}")


def write_private(path: str, content: bytes):
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | close_on_exec, 0o600
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)

os.mkdir(snapshot_dir, 0o700)
limits = {
    "manifest.json": 1024 * 1024,
    "SHA256SUMS": 64 * 1024,
    "local-image-bundle-manifest.schema.json": 4 * 1024 * 1024,
    "import-local-image-bundle.sh": 4 * 1024 * 1024,
    archive_name: archive_size,
}
expected_digests = dict(entries)
expected_digests["SHA256SUMS"] = release_checksums_sha
for name in sorted(expected_physical):
    copy_verified(
        name,
        expected_digests[name],
        limits[name],
        archive_size if name == archive_name else None,
    )

try:
    final_names = os.listdir(source_fd)
except OSError as exc:
    raise SystemExit(f"cannot re-enumerate bundle directory: {exc}")
finally:
    os.close(source_fd)
if len(final_names) != len(set(final_names)) or set(final_names) != expected_physical:
    missing = sorted(expected_physical - set(final_names))
    extra = sorted(set(final_names) - expected_physical)
    raise SystemExit(
        f"bundle physical entry set changed; missing={missing}, unexpected={extra}"
    )
write_private(release_snapshot, release_bytes)
PY
} 2>&1)" || die "unsafe or invalid bundle: ${snapshot_error}"

bundle_dir="${snapshot_dir}"
manifest_path="${bundle_dir}/manifest.json"
checksum_path="${bundle_dir}/SHA256SUMS"
(
    cd -- "${bundle_dir}"
    sha256sum --check --strict -- SHA256SUMS
) || die 'bundle checksum verification failed'

fields="$({
    python3 - "${manifest_path}" "${checksum_path}" "${release_snapshot}" <<'PY'
import hashlib
import json
import re
import sys

manifest_path, checksum_path, release_path = sys.argv[1:]
with open(manifest_path, "r", encoding="utf-8") as handle:
    manifest = json.load(handle)
with open(release_path, "r", encoding="utf-8") as handle:
    release_manifest = json.load(handle)
if not isinstance(manifest, dict):
    raise SystemExit("manifest root must be an object")
if manifest.get("$schema") != "local-image-bundle-manifest.schema.json":
    raise SystemExit("unsupported manifest schema reference")
if manifest.get("schema_version") != 1:
    raise SystemExit("unsupported manifest schema_version")
if set(manifest) != {"$schema", "schema_version", "created_at", "image", "archive"}:
    raise SystemExit("manifest has missing or unknown top-level fields")
if not isinstance(release_manifest, dict) or set(release_manifest) != {
    "$schema", "schema_version", "release", "profile", "components", "verification"
}:
    raise SystemExit("release manifest has missing or unknown top-level fields")
if release_manifest.get("$schema") != "release-manifest.schema.json":
    raise SystemExit("unsupported release manifest schema reference")
if release_manifest.get("schema_version") != 1:
    raise SystemExit("unsupported release manifest schema_version")
if release_manifest.get("profile") != "aliyun-hydro5-pm2-direct-v1":
    raise SystemExit("unsupported release profile")

release = release_manifest.get("release")
components = release_manifest.get("components")
if not isinstance(release, dict) or set(release) != {
    "version", "git_revision", "created_at"
}:
    raise SystemExit("release metadata has missing or unknown fields")
if not isinstance(components, dict) or set(components) != {
    "orchestrator", "hydro_plugin", "desktop"
}:
    raise SystemExit("release components have missing or unknown fields")
desktop = components.get("desktop")
desktop_fields = {
    "delivery",
    "bundle_manifest_sha256",
    "bundle_checksums_sha256",
    "source_revision",
    "image_tag",
    "image_id",
    "contract",
    "iso_sha256",
}
if not isinstance(desktop, dict) or set(desktop) != desktop_fields:
    raise SystemExit("release desktop object has missing or unknown fields")
if desktop.get("delivery") != "offline":
    raise SystemExit("release desktop delivery must be offline")
if not isinstance(manifest.get("created_at"), str) or not re.fullmatch(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
    manifest["created_at"],
):
    raise SystemExit("created_at must be a UTC RFC3339 timestamp without fractional seconds")

image = manifest.get("image")
archive = manifest.get("archive")
if not isinstance(image, dict) or set(image) != {
    "tag", "id", "source_revision", "labels"
}:
    raise SystemExit("manifest image object has missing or unknown fields")
if not isinstance(archive, dict) or set(archive) != {
    "file", "format", "compression", "sha256", "size_bytes"
}:
    raise SystemExit("manifest archive object has missing or unknown fields")

tag = image.get("tag")
image_id = image.get("id")
source_revision = image.get("source_revision")
labels = image.get("labels")
filename = archive.get("file")
compression = archive.get("compression")
checksum = archive.get("sha256")
size = archive.get("size_bytes")

if not isinstance(tag, str) or not re.fullmatch(
    r"(?:[A-Za-z0-9._-]+(?::[0-9]+)?/)*"
    r"[A-Za-z0-9._-]+:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}",
    tag,
):
    raise SystemExit("invalid image tag")
if "@" in tag or ":" not in tag.rsplit("/", 1)[-1] or tag.rsplit(":", 1)[-1] == "latest":
    raise SystemExit("image tag must be a fixed non-latest version tag")
if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
    raise SystemExit("invalid image ID")
if not isinstance(source_revision, str) or not re.fullmatch(
    r"[a-f0-9]{40}", source_revision
):
    raise SystemExit("invalid image source_revision")
if not isinstance(labels, dict) or not all(
    isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
):
    raise SystemExit("image labels must be a string map")
if labels.get("org.noi.desktop.contract") != "finalizer-status-v1":
    raise SystemExit("invalid desktop contract label")
if not re.fullmatch(r"[a-f0-9]{64}", labels.get("org.noi.iso.sha256", "")):
    raise SystemExit("invalid ISO SHA256 label")
if labels.get("org.opencontainers.image.revision") != source_revision:
    raise SystemExit("image revision label does not match image source_revision")
if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9._-]+\.tar(?:\.zst)?", filename):
    raise SystemExit("invalid archive filename")
if archive.get("format") != "docker-archive":
    raise SystemExit("unsupported archive format")
if compression not in {"none", "zstd"}:
    raise SystemExit("unsupported archive compression")
if compression == "none" and not filename.endswith(".tar"):
    raise SystemExit("uncompressed archive filename must end in .tar")
if compression == "zstd" and not filename.endswith(".tar.zst"):
    raise SystemExit("zstd archive filename must end in .tar.zst")
if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
    raise SystemExit("invalid archive SHA256")
if isinstance(size, bool) or not isinstance(size, int) or size < 1:
    raise SystemExit("invalid archive size")

with open(manifest_path, "rb") as handle:
    release_manifest_digest = hashlib.sha256(handle.read()).hexdigest()
with open(checksum_path, "rb") as handle:
    release_checksums_digest = hashlib.sha256(handle.read()).hexdigest()
for field_name in ("bundle_manifest_sha256", "bundle_checksums_sha256", "iso_sha256"):
    value = desktop.get(field_name)
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise SystemExit(f"release desktop has invalid {field_name}")
release_source_revision = desktop.get("source_revision")
release_image_tag = desktop.get("image_tag")
release_image_id = desktop.get("image_id")
if not isinstance(release_source_revision, str) or not re.fullmatch(
    r"[a-f0-9]{40}", release_source_revision
):
    raise SystemExit("release desktop has invalid source_revision")
if not isinstance(release_image_tag, str) or not re.fullmatch(
    r"(?:[A-Za-z0-9._-]+(?::[0-9]+)?/)*"
    r"[A-Za-z0-9._-]+:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}",
    release_image_tag,
):
    raise SystemExit("release desktop has invalid image_tag")
if release_image_tag.rsplit(":", 1)[-1] == "latest":
    raise SystemExit("release desktop image_tag must be a fixed non-latest tag")
if not isinstance(release_image_id, str) or not re.fullmatch(
    r"sha256:[a-f0-9]{64}", release_image_id
):
    raise SystemExit("release desktop has invalid image_id")
if release.get("git_revision") != release_source_revision:
    raise SystemExit("release git_revision does not match desktop source_revision")
comparisons = {
    "bundle manifest SHA256": (
        release_manifest_digest,
        desktop["bundle_manifest_sha256"],
    ),
    "SHA256SUMS SHA256": (
        release_checksums_digest,
        desktop["bundle_checksums_sha256"],
    ),
    "source revision": (source_revision, release_source_revision),
    "image tag": (tag, release_image_tag),
    "image ID": (image_id, release_image_id),
    "desktop contract": (
        labels["org.noi.desktop.contract"],
        desktop.get("contract"),
    ),
    "ISO SHA256": (labels["org.noi.iso.sha256"], desktop["iso_sha256"]),
}
for label, (bundle_value, release_value) in comparisons.items():
    if bundle_value != release_value:
        raise SystemExit(f"bundle {label} does not match the public Release manifest")

print("\t".join((tag, image_id, filename, compression, checksum, str(size))))
PY
} 2>&1)" || die "invalid manifest: ${fields}"

IFS=$'\t' read -r image_tag expected_id archive_file compression expected_sha256 expected_size \
    <<<"${fields}"
archive_path="${bundle_dir}/${archive_file}"
[[ -f "${archive_path}" ]] || die "archive not found: ${archive_path}"
if [[ "${compression}" == 'zstd' ]]; then
    need_cmd zstd
fi

actual_size="$(stat -c '%s' -- "${archive_path}")"
[[ "${actual_size}" == "${expected_size}" ]] \
    || die "archive size mismatch: expected ${expected_size}, got ${actual_size}"
actual_sha256="$(sha256sum -- "${archive_path}" | awk '{print $1}')"
[[ "${actual_sha256}" == "${expected_sha256}" ]] \
    || die "archive SHA256 mismatch: expected ${expected_sha256}, got ${actual_sha256}"

archive_config_file="${tmp_dir}/docker-image-config.json"
if [[ "${compression}" == 'zstd' ]]; then
    archive_identity="$({
        zstd -dc -- "${archive_path}" \
            | python3 "${archive_identity_verifier}" \
                --archive - \
                --expected-tag "${image_tag}" \
                --expected-image-id "${expected_id}" \
                --config-output "${archive_config_file}"
    } 2>&1)" || die "invalid Docker archive: ${archive_identity}"
else
    archive_identity="$({
        python3 "${archive_identity_verifier}" \
            --archive "${archive_path}" \
            --expected-tag "${image_tag}" \
            --expected-image-id "${expected_id}" \
            --config-output "${archive_config_file}"
    } 2>&1)" || die "invalid Docker archive: ${archive_identity}"
fi
printf '%s\n' "${archive_identity}"

config_size="$(stat -c '%s' -- "${archive_config_file}")"
((config_size > 0 && config_size <= 4194304)) \
    || die 'Docker archive config must be between 1 byte and 4 MiB'

# Authenticate the archive's actual labels before the first Docker daemon call;
# checking only the bundle manifest would allow it to describe different bytes.
config_error="$({
    python3 - "${manifest_path}" "${archive_config_file}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    expected = json.load(handle)["image"]
with open(sys.argv[2], "r", encoding="utf-8") as handle:
    archive_config = json.load(handle)
actual_labels = archive_config.get("config", {}).get("Labels")
if not isinstance(actual_labels, dict) or not all(
    isinstance(key, str) and isinstance(value, str)
    for key, value in actual_labels.items()
):
    raise SystemExit("Docker archive config labels are not a string map")
if actual_labels != expected["labels"]:
    changed = sorted(
        key for key, value in expected["labels"].items()
        if actual_labels.get(key) != value
    )
    extra = sorted(key for key in actual_labels if key not in expected["labels"])
    raise SystemExit(
        "Docker archive config labels do not match bundle manifest; "
        f"changed_or_missing={changed}, unexpected={extra}"
    )
if actual_labels.get("org.opencontainers.image.revision") != expected["source_revision"]:
    raise SystemExit("Docker archive revision label does not match source_revision")
PY
} 2>&1)" || die "invalid Docker archive config: ${config_error}"

verify_loaded_image() {
    local actual_id inspect_file
    actual_id="$(docker image inspect "${image_tag}" --format '{{.Id}}')" \
        || die "loaded tag is missing: ${image_tag}"
    [[ "${actual_id}" == "${expected_id}" ]] \
        || die "loaded image ID mismatch: expected ${expected_id}, got ${actual_id}"
    inspect_file="${tmp_dir}/loaded-image-inspect.json"
    docker image inspect "${actual_id}" >"${inspect_file}"
    python3 - "${manifest_path}" "${inspect_file}" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    expected = json.load(handle)["image"]["labels"]
with open(sys.argv[2], "r", encoding="utf-8") as handle:
    inspected = json.load(handle)
if not isinstance(inspected, list) or len(inspected) != 1:
    raise SystemExit("docker inspect did not return exactly one loaded image")
actual = (inspected[0].get("Config") or {}).get("Labels") or {}
if actual != expected:
    missing = sorted(key for key in expected if actual.get(key) != expected[key])
    extra = sorted(key for key in actual if key not in expected)
    raise SystemExit(
        "loaded image labels do not match manifest; "
        f"changed_or_missing={missing}, unexpected={extra}"
    )
PY
}

inspect_tag_id=''
inspect_image_tag() {
    local inspect_error inspect_output
    inspect_output="${tmp_dir}/tag-inspect.out"
    inspect_error="${tmp_dir}/tag-inspect.err"
    if docker image inspect "${image_tag}" --format '{{.Id}}' \
        >"${inspect_output}" 2>"${inspect_error}"; then
        IFS= read -r inspect_tag_id <"${inspect_output}"
        [[ "${inspect_tag_id}" =~ ^sha256:[a-f0-9]{64}$ ]] \
            || die "Docker returned an invalid image ID for ${image_tag}"
        return 0
    fi
    if grep -Eq '^(Error response from daemon: |Error: )?No such image: ' "${inspect_error}"; then
        inspect_tag_id=''
        return 2
    fi
    printf 'error: Docker could not determine whether %s exists:\n' "${image_tag}" >&2
    sed 's/^/  /' "${inspect_error}" >&2
    return 1
}

tag_inspect_status=0
inspect_image_tag || tag_inspect_status=$?
if ((tag_inspect_status == 0)); then
    previous_id="${inspect_tag_id}"
    if [[ "${previous_id}" == "${expected_id}" ]]; then
        verify_loaded_image
        printf 'Image already present and verified: %s (%s)\n' "${image_tag}" "${expected_id}"
        exit 0
    fi
    ((replace_existing == 1)) \
        || die "${image_tag} already points to ${previous_id}; use --replace-existing only after checking active seats"
    restore_needed=1
elif ((tag_inspect_status == 2)); then
    remove_loaded_tag=1
else
    die "refusing to load while the current state of ${image_tag} is unknown"
fi

printf 'Loading %s (%s)...\n' "${image_tag}" "${expected_id}"
if [[ "${compression}" == 'zstd' ]]; then
    zstd -dc -- "${archive_path}" | docker image load
else
    docker image load --input "${archive_path}"
fi

verify_loaded_image
restore_needed=0
remove_loaded_tag=0
printf 'Image loaded and verified: %s (%s)\n' "${image_tag}" "${expected_id}"
printf 'Archive SHA256: %s\n' "${expected_sha256}"
