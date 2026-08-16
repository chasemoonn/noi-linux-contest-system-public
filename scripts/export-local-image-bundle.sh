#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
    cat <<'EOF'
Usage:
  export-local-image-bundle.sh --image NAME:VERSION --source-revision GIT_COMMIT
      [--compression none|zstd] [--bundle-dir PATH]

The default bundle directory is local-release/NAME_VERSION under the repository.
The destination must not already exist.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

image_tag=''
source_revision=''
compression='none'
bundle_dir=''

while (($#)); do
    case "$1" in
        --image)
            (($# >= 2)) || die '--image requires a value'
            image_tag="$2"
            shift 2
            ;;
        --compression)
            (($# >= 2)) || die '--compression requires a value'
            compression="$2"
            shift 2
            ;;
        --source-revision)
            (($# >= 2)) || die '--source-revision requires a value'
            source_revision="$2"
            shift 2
            ;;
        --bundle-dir)
            (($# >= 2)) || die '--bundle-dir requires a value'
            bundle_dir="$2"
            shift 2
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

[[ -n "${image_tag}" ]] || die '--image is required'
[[ "${source_revision}" =~ ^[a-f0-9]{40}$ ]] \
    || die '--source-revision must be the exact 40-character Git commit used to build the image'
[[ "${image_tag}" != *@* ]] || die '--image must be a versioned tag, not a registry digest'
image_leaf="${image_tag##*/}"
[[ "${image_leaf}" == *:* ]] || die '--image must contain an explicit version tag'
image_version="${image_leaf##*:}"
[[ -n "${image_version}" && "${image_version}" != 'latest' ]] \
    || die '--image must use a fixed non-latest version tag'
[[ "${compression}" == 'none' || "${compression}" == 'zstd' ]] \
    || die '--compression must be none or zstd'

for command_name in docker python3 tar sha256sum stat mktemp awk cp mv rm; do
    need_cmd "${command_name}"
done
if [[ "${compression}" == 'zstd' ]]; then
    need_cmd zstd
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "${script_dir}/.." && pwd)"
schema_source="${repo_dir}/release/local-image-bundle-manifest.schema.json"
import_source="${script_dir}/import-local-image-bundle.sh"
archive_identity_verifier="${script_dir}/verify_docker_archive_identity.py"
[[ -f "${schema_source}" ]] || die "manifest schema not found: ${schema_source}"
[[ -f "${import_source}" ]] || die "import script not found: ${import_source}"
[[ -f "${archive_identity_verifier}" ]] \
    || die "archive identity verifier not found: ${archive_identity_verifier}"

safe_name="${image_tag//\//_}"
safe_name="${safe_name//:/_}"
if [[ -z "${bundle_dir}" ]]; then
    bundle_dir="${repo_dir}/local-release/${safe_name}"
fi
bundle_dir="${bundle_dir%/}"
[[ -n "${bundle_dir}" ]] || die 'bundle directory cannot be empty'
[[ ! -e "${bundle_dir}" ]] || die "bundle directory already exists: ${bundle_dir}"

bundle_parent="$(dirname -- "${bundle_dir}")"
mkdir -p -- "${bundle_parent}"
tmp_dir="$(mktemp -d -- "${bundle_dir}.tmp.XXXXXX")"
cleanup() {
    if [[ -n "${tmp_dir:-}" && -d "${tmp_dir}" ]]; then
        rm -rf -- "${tmp_dir}"
    fi
}
trap cleanup EXIT

inspect_file="${tmp_dir}/image-inspect.json"
docker image inspect "${image_tag}" >"${inspect_file}" \
    || die "Docker image not found: ${image_tag}"

image_id="$({
    python3 - "${inspect_file}" "${source_revision}" <<'PY'
import json
import re
import sys

inspect_path, expected_revision = sys.argv[1:]
with open(inspect_path, "r", encoding="utf-8") as handle:
    document = json.load(handle)
if not isinstance(document, list) or len(document) != 1:
    raise SystemExit("docker inspect did not return exactly one image")
image = document[0]
image_id = image.get("Id")
if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
    raise SystemExit("image has an invalid Docker image ID")
labels = (image.get("Config") or {}).get("Labels") or {}
if not isinstance(labels, dict) or not all(
    isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
):
    raise SystemExit("image labels are not a string map")
if labels.get("org.noi.desktop.contract") != "finalizer-status-v1":
    raise SystemExit("image is missing org.noi.desktop.contract=finalizer-status-v1")
if not re.fullmatch(r"[a-f0-9]{64}", labels.get("org.noi.iso.sha256", "")):
    raise SystemExit("image is missing a valid org.noi.iso.sha256 label")
if labels.get("org.opencontainers.image.revision") != expected_revision:
    raise SystemExit(
        "image org.opencontainers.image.revision must exactly match --source-revision"
    )
print(image_id)
PY
} 2>&1)" || die "${image_id}"

archive_tar="${tmp_dir}/${safe_name}.tar"
printf 'Saving %s (%s)...\n' "${image_tag}" "${image_id}"
docker image save --output "${archive_tar}" "${image_tag}"

after_save_id="$(docker image inspect "${image_tag}" --format '{{.Id}}')"
[[ "${after_save_id}" == "${image_id}" ]] \
    || die "image tag changed during export: ${image_id} -> ${after_save_id}"

archive_identity="$({
    python3 "${archive_identity_verifier}" \
        --archive "${archive_tar}" \
        --expected-tag "${image_tag}" \
        --expected-image-id "${image_id}"
} 2>&1)" || die "invalid Docker archive: ${archive_identity}"
printf '%s\n' "${archive_identity}"

if [[ "${compression}" == 'zstd' ]]; then
    archive_file="${safe_name}.tar.zst"
    archive_path="${tmp_dir}/${archive_file}"
    printf 'Compressing with zstd...\n'
    zstd -T0 -19 -q -f -o "${archive_path}" -- "${archive_tar}"
    rm -f -- "${archive_tar}"
else
    archive_file="${safe_name}.tar"
    archive_path="${archive_tar}"
fi

archive_sha256="$(sha256sum -- "${archive_path}" | awk '{print $1}')"
archive_size="$(stat -c '%s' -- "${archive_path}")"
manifest_path="${tmp_dir}/manifest.json"

python3 - \
    "${inspect_file}" "${manifest_path}" "${image_tag}" "${image_id}" \
    "${source_revision}" "${archive_file}" "${compression}" \
    "${archive_sha256}" "${archive_size}" <<'PY'
from datetime import datetime, timezone
import json
import sys

(
    inspect_path,
    manifest_path,
    image_tag,
    image_id,
    source_revision,
    archive_file,
    compression,
    archive_sha256,
    archive_size,
) = sys.argv[1:]
with open(inspect_path, "r", encoding="utf-8") as handle:
    labels = (json.load(handle)[0].get("Config") or {}).get("Labels") or {}
manifest = {
    "$schema": "local-image-bundle-manifest.schema.json",
    "schema_version": 1,
    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "image": {
        "tag": image_tag,
        "id": image_id,
        "source_revision": source_revision,
        "labels": labels,
    },
    "archive": {
        "file": archive_file,
        "format": "docker-archive",
        "compression": compression,
        "sha256": archive_sha256,
        "size_bytes": int(archive_size),
    },
}
with open(manifest_path, "x", encoding="utf-8", newline="\n") as handle:
    json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
    handle.write("\n")
PY

cp -- "${schema_source}" "${tmp_dir}/local-image-bundle-manifest.schema.json"
cp -- "${import_source}" "${tmp_dir}/import-local-image-bundle.sh"
chmod 0700 -- "${tmp_dir}/import-local-image-bundle.sh"
(
    cd -- "${tmp_dir}"
    sha256sum -- \
        "${archive_file}" \
        manifest.json \
        local-image-bundle-manifest.schema.json \
        import-local-image-bundle.sh \
        >SHA256SUMS
)
manifest_sha256="$(sha256sum -- "${manifest_path}" | awk '{print $1}')"
checksums_sha256="$(sha256sum -- "${tmp_dir}/SHA256SUMS" | awk '{print $1}')"
rm -f -- "${inspect_file}"
mv -T -- "${tmp_dir}" "${bundle_dir}"
tmp_dir=''
trap - EXIT

printf 'Bundle created: %s\n' "${bundle_dir}"
printf 'Image tag:      %s\n' "${image_tag}"
printf 'Image ID:       %s\n' "${image_id}"
printf 'Source commit:  %s\n' "${source_revision}"
printf 'Archive SHA256: %s\n' "${archive_sha256}"
printf 'Manifest SHA256:  %s\n' "${manifest_sha256}"
printf 'Checksums SHA256: %s\n' "${checksums_sha256}"
