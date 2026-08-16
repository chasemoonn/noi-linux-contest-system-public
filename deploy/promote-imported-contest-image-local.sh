#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
    cat <<'EOF'
Usage:
  promote-imported-contest-image-local.sh --image NAME:VERSION \
      --expected-image-id sha256:HEX --source-root PATH \
      --source-revision GIT_COMMIT

Promote an already imported, immutable NOI Linux image together with a source
snapshot from the exact Git revision. The host must be quiescent and already
have a distinct managed formal baseline. This script never builds an image.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

image=''
expected_image_id=''
source_root=''
source_revision=''
original_arguments=("$@")
qualification_marker="${NOI_V1_QUALIFICATION_MARKER:-}"
qualification_ready_path="${NOI_V1_POWER_LOSS_READY_PATH:-}"
if [[ -n "${qualification_marker}" || -n "${qualification_ready_path}" ]]; then
    [[ "${qualification_marker}" =~ ^NOI-V1-QUAL-[A-Z0-9]{16,64}$ \
        && "${qualification_ready_path}" =~ ^/root/[A-Za-z0-9._/-]+[.]json$ \
        && "${qualification_ready_path}" != *'//'* \
        && "${qualification_ready_path}" != *'/../'* ]] \
        || die 'qualification power-loss hook identity is invalid'
fi
while (($#)); do
    case "$1" in
        --image)
            (($# >= 2)) || die '--image requires a value'
            image="$2"
            shift 2
            ;;
        --source-root)
            (($# >= 2)) || die '--source-root requires a value'
            source_root="$2"
            shift 2
            ;;
        --expected-image-id)
            (($# >= 2)) || die '--expected-image-id requires a value'
            expected_image_id="$2"
            shift 2
            ;;
        --source-revision)
            (($# >= 2)) || die '--source-revision requires a value'
            source_revision="$2"
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

[[ "$(id -u)" == 0 ]] || die 'run as root'
[[ "${image}" =~ ^([A-Za-z0-9._-]+(:[0-9]+)?/)*[A-Za-z0-9._-]+:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ \
    && "${image}" != *@* ]] || die '--image must be a safe versioned tag'
image_leaf="${image##*/}"
[[ "${image_leaf}" == *:* && "${image_leaf##*:}" != latest ]] \
    || die '--image must use an explicit non-latest tag'
[[ "${source_revision}" =~ ^[a-f0-9]{40}$ ]] \
    || die '--source-revision must be 40 lowercase hexadecimal characters'
[[ "${expected_image_id}" =~ ^sha256:[a-f0-9]{64}$ ]] \
    || die '--expected-image-id must be an immutable Docker image ID'
[[ -n "${source_root}" ]] || die '--source-root is required'

for command_name in docker git tar flock awk install mktemp readlink stat sync python3 \
    chmod chown date od tr find grep rm mv ln; do
    need_cmd "${command_name}"
done

lock_file=/var/lock/noi-official-image-deploy.lock
if [[ "${NOI_IMPORTED_PROMOTION_LOCK_HELD:-}" != 1 ]]; then
    exec python3 - "${lock_file}" "$0" "${original_arguments[@]}" <<'PY'
import fcntl
import os
import stat
import sys

lock_path, script, *arguments = sys.argv[1:]
flags = os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(lock_path, flags)
except OSError as exc:
    raise SystemExit(f"shared image deployment lock cannot be opened safely: {exc}")
info = os.fstat(descriptor)
allowed_modes = {0o600, 0o644}
if (
    not stat.S_ISREG(info.st_mode)
    or info.st_uid != 0
    or info.st_gid != 0
    or info.st_nlink != 1
    or stat.S_IMODE(info.st_mode) not in allowed_modes
):
    raise SystemExit("shared image deployment lock metadata is unsafe")
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("another image deployment, verification, or rollback is running")
os.fchmod(descriptor, 0o600)
os.dup2(descriptor, 8, inheritable=True)
os.set_inheritable(8, True)
if descriptor != 8:
    os.close(descriptor)
environment = os.environ.copy()
environment["NOI_IMPORTED_PROMOTION_LOCK_HELD"] = "1"
os.execve("/bin/bash", ["bash", script, *arguments], environment)
PY
fi
python3 - "${lock_file}" <<'PY'
import fcntl
import os
import stat
import sys

path = sys.argv[1]
try:
    descriptor_info = os.fstat(8)
    path_info = os.stat(path, follow_symlinks=False)
except OSError as exc:
    raise SystemExit(f"shared image deployment lock guardian is absent: {exc}")
if (
    not stat.S_ISREG(path_info.st_mode)
    or stat.S_ISLNK(path_info.st_mode)
    or path_info.st_uid != 0
    or path_info.st_gid != 0
    or path_info.st_nlink != 1
    or stat.S_IMODE(path_info.st_mode) != 0o600
    or (descriptor_info.st_dev, descriptor_info.st_ino)
       != (path_info.st_dev, path_info.st_ino)
):
    raise SystemExit("shared image deployment lock guardian does not match the fixed lock")
try:
    fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("another image deployment, verification, or rollback is running")
PY

source_root="$(cd -- "${source_root}" && pwd -P)"
[[ ! -L "${source_root}" && "$(stat -c '%u' -- "${source_root}")" == 0 \
    && "$(stat -c '%a' -- "${source_root}")" =~ ^[0-7][0145][0145]$ ]] \
    || die 'source root must be root-owned and not group/other-writable'
[[ -d "${source_root}/.git" && ! -L "${source_root}/.git" ]] \
    || die 'source checkout must contain a real .git directory'
if find "${source_root}/.git" -xdev \
    \( ! -user root -o -perm /022 \) -print -quit | grep -q .; then
    die 'Git metadata must be root-owned and not group/other-writable'
fi
[[ "$(git -C "${source_root}" rev-parse HEAD)" == "${source_revision}" ]] \
    || die 'source checkout HEAD differs from --source-revision'
[[ -z "$(git -C "${source_root}" status --porcelain=v1 --untracked-files=no)" ]] \
    || die 'tracked source checkout is dirty'
for required in \
    noi-linux-official \
    deploy/verify-contest-image-local.sh \
    deploy/rollback-contest-image-local.sh; do
    git -C "${source_root}" cat-file -e "${source_revision}:${required}" \
        || die "required source path is absent at revision: ${required}"
done

app="${NOI_APP_ROOT:-/opt/noi-linux-contest-system}"
[[ "${app}" =~ ^/[A-Za-z0-9._/-]+$ && "${app}" != / \
    && "${app}" != *'//'* && "${app}" != *'/../'* && "${app}" != *'/..' ]] \
    || die 'NOI_APP_ROOT must be a safe absolute non-root path'
[[ "$(readlink -f -- "${app}")" == "${app}" && -d "${app}" && ! -L "${app}" ]] \
    || die 'NOI_APP_ROOT must already be a real canonical directory'
cursor=''
IFS='/' read -r -a app_parts <<< "${app#/}"
for part in "${app_parts[@]}"; do
    cursor="${cursor}/${part}"
    directory_stat="$(stat -c '%u:%a' -- "${cursor}")"
    [[ -d "${cursor}" && ! -L "${cursor}" \
        && "${directory_stat}" =~ ^0:[0-7][0145][0145]$ ]] \
        || die "unsafe application ancestor: ${cursor}"
done
install -d -m 0755 -- "${app}/image-releases"

if [[ -n "$(docker ps -q --filter label=noi.contest)" ]]; then
    die 'contest seat containers are running; promotion is refused'
fi

current_link="${app}/current-image-source"
pending="${app}/image-promotion.pending"
[[ ! -e "${pending}" && ! -L "${pending}" ]] \
    || die "unfinished image transaction found at ${pending}"
[[ -L "${current_link}" ]] \
    || die 'a distinct managed formal baseline is required before imported-image promotion'

read_value() {
    local file="$1"
    local key="$2"
    awk -F= -v wanted="${key}" \
        '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "${file}"
}

valid_image_id() {
    [[ "$1" =~ ^sha256:[a-f0-9]{64}$ ]]
}

valid_source_target() {
    [[ "$1" =~ ^image-releases/[A-Za-z0-9TZ-]+$ ]]
}

formal_image_id() {
    docker image inspect noi-linux-official:2.0 --format '{{.Id}}' 2>/dev/null || true
}

assert_pair() {
    local expected_source="$1"
    local expected_image="$2"
    local metadata recorded_source recorded_image actual_image
    valid_source_target "${expected_source}" \
        && [[ -d "${app}/${expected_source}" ]] \
        || return 1
    metadata="${app}/${expected_source}/promotion.env"
    [[ -r "${metadata}" ]] || return 1
    recorded_source="$(read_value "${metadata}" SOURCE_TARGET)"
    recorded_image="$(read_value "${metadata}" PROMOTED_IMAGE_ID)"
    actual_image="$(formal_image_id)"
    [[ "${recorded_source}" == "${expected_source}" \
        && "${recorded_image}" == "${expected_image}" \
        && "${actual_image}" == "${expected_image}" ]]
}

old_source="$(readlink "${current_link}")"
valid_source_target "${old_source}" || die 'current source target is unsafe'
old_metadata="${app}/${old_source}/promotion.env"
[[ -r "${old_metadata}" ]] || die 'current source metadata is missing'
old_image="$(read_value "${old_metadata}" PROMOTED_IMAGE_ID)"
valid_image_id "${old_image}" || die 'current formal image ID is invalid'
assert_pair "${old_source}" "${old_image}" \
    || die 'current formal image and source metadata are not a recorded pair'

candidate_id="$(docker image inspect "${image}" --format '{{.Id}}')" \
    || die 'imported candidate tag is absent'
valid_image_id "${candidate_id}" || die 'candidate image ID is invalid'
[[ "${candidate_id}" == "${expected_image_id}" ]] \
    || die 'candidate tag differs from --expected-image-id'
[[ "${candidate_id}" != "${old_image}" ]] \
    || die 'candidate image must differ from the formal baseline'
candidate_revision="$(docker image inspect "${candidate_id}" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
candidate_contract="$(docker image inspect "${candidate_id}" \
    --format '{{index .Config.Labels "org.noi.desktop.contract"}}')"
candidate_iso="$(docker image inspect "${candidate_id}" \
    --format '{{index .Config.Labels "org.noi.iso.sha256"}}')"
[[ "${candidate_revision}" == "${source_revision}" ]] \
    || die 'candidate revision label differs from the source revision'
[[ "${candidate_contract}" == finalizer-status-v1 ]] \
    || die 'candidate desktop contract differs'
[[ "${candidate_iso}" =~ ^[a-f0-9]{64}$ ]] \
    || die 'candidate ISO label is invalid'

release_id="$(date -u +%Y%m%dT%H%M%SZ)-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
[[ "${release_id}" =~ ^[A-Za-z0-9TZ-]+$ ]] || die 'generated release ID is invalid'
stage="$(mktemp -d -- "${app}/.imported-image-stage-${release_id}.XXXXXX")"
release_root="${app}/image-releases/${release_id}"
new_source="image-releases/${release_id}"
next_link="${app}/.current-image-source-${release_id}"
rollback_tag="noi-linux-official:rollback-${release_id}"
stage_active=1
next_link_active=0

cleanup() {
    if [[ "${next_link_active}" == 1 ]]; then
        rm -f -- "${next_link}"
    fi
    if [[ "${stage_active}" == 1 ]]; then
        rm -rf -- "${stage}"
    fi
}
trap cleanup EXIT

git -C "${source_root}" archive --format=tar "${source_revision}" \
    noi-linux-official deploy \
    | tar -xf - -C "${stage}"
if find "${stage}" -type l -print -quit | grep -q .; then
    die 'source snapshot contains a symlink'
fi
chmod -R go-w -- "${stage}"
bash -n "${stage}/deploy/verify-contest-image-local.sh"
bash -n "${stage}/deploy/rollback-contest-image-local.sh"
bash "${stage}/deploy/verify-contest-image-local.sh" \
    "${candidate_id}" "${source_revision}"
[[ "$(docker image inspect "${image}" --format '{{.Id}}')" == "${candidate_id}" ]] \
    || die 'candidate tag changed during verification'
[[ -z "$(docker ps -q --filter label=noi.contest)" ]] \
    || die 'contest seats started during candidate verification'
[[ ! -e "${release_root}" ]] || die 'generated release target already exists'

docker tag "${old_image}" "${rollback_tag}"
[[ "$(docker image inspect "${rollback_tag}" --format '{{.Id}}')" == "${old_image}" ]] \
    || die 'rollback tag does not preserve the formal baseline'

{
    printf 'PROMOTED_IMAGE_ID=%s\n' "${candidate_id}"
    printf 'SOURCE_TARGET=%s\n' "${new_source}"
    printf 'SOURCE_REVISION=%s\n' "${source_revision}"
    printf 'ROLLBACK_TAG=%s\n' "${rollback_tag}"
    printf 'ROLLBACK_IMAGE_ID=%s\n' "${old_image}"
    printf 'ROLLBACK_SOURCE_TARGET=%s\n' "${old_source}"
} > "${stage}/promotion.env"
chmod 0644 "${stage}/promotion.env"
python3 - "${stage}" <<'PY'
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
directories = []
for current, names, files in os.walk(root, topdown=True, followlinks=False):
    current_path = Path(current)
    directories.append(current_path)
    for name in names:
        path = current_path / name
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise SystemExit(f"unsafe source directory entry: {path}")
    for name in files:
        path = current_path / name
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SystemExit(f"unsafe source file: {path}")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
for path in reversed(directories):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
mv -- "${stage}" "${release_root}"
stage_active=0
sync -f "${app}/image-releases"
ln -s "${new_source}" "${next_link}"
next_link_active=1

promotion_active=0
restore_failed=0
rollback_promotion() {
    local rc="${1:-$?}"
    trap - ERR HUP INT TERM
    set +e
    if [[ "${promotion_active}" == 1 ]]; then
        docker tag "${old_image}" noi-linux-official:2.0 || restore_failed=1
        restore_link="${app}/.restore-image-source-${release_id}"
        rm -f -- "${restore_link}"
        ln -s "${old_source}" "${restore_link}" || restore_failed=1
        if [[ "${restore_failed}" == 0 ]]; then
            mv -Tf -- "${restore_link}" "${current_link}" || restore_failed=1
            assert_pair "${old_source}" "${old_image}" || restore_failed=1
        fi
        if [[ "${restore_failed}" == 0 ]]; then
            rm -f -- "${pending}"
            sync -f "${app}" || true
        else
            printf 'promotion recovery incomplete; marker retained: %s\n' "${pending}" >&2
        fi
    fi
    exit "${rc}"
}
trap 'rollback_promotion $?' ERR
trap 'rollback_promotion 129' HUP
trap 'rollback_promotion 130' INT
trap 'rollback_promotion 143' TERM

[[ -z "$(docker ps -q --filter label=noi.contest)" ]] \
    || die 'contest seats started before transaction commit'
[[ "$(docker image inspect "${image}" --format '{{.Id}}')" == "${candidate_id}" ]] \
    || die 'candidate tag changed before transaction commit'
assert_pair "${old_source}" "${old_image}" \
    || die 'formal baseline changed before transaction commit'

transaction_temp="${pending}.${release_id}"
{
    printf 'TXN_VERSION=1\n'
    printf 'OLD_IMAGE_PRESENT=1\n'
    printf 'OLD_IMAGE_ID=%s\n' "${old_image}"
    printf 'OLD_SOURCE_TARGET=%s\n' "${old_source}"
    printf 'NEW_IMAGE_ID=%s\n' "${candidate_id}"
    printf 'NEW_SOURCE_TARGET=%s\n' "${new_source}"
    printf 'NEW_SOURCE_REVISION=%s\n' "${source_revision}"
} > "${transaction_temp}"
chmod 0600 "${transaction_temp}"
sync -f "${transaction_temp}"
mv -Tf -- "${transaction_temp}" "${pending}"
sync -f "${app}"

# Qualification-only deterministic crash boundary.  The process stops after
# the durable recovery marker exists and before either the formal image tag or
# source link changes.  An external trusted action agent must observe the
# stopped process and SIGKILL it; continuing it is deliberately refused.
if [[ -n "${qualification_marker}" ]]; then
    python3 - "${qualification_ready_path}" "${qualification_marker}" "$$" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

path = Path(sys.argv[1])
parent = path.parent.resolve(strict=True)
if parent != path.parent or parent.stat().st_uid != 0 or stat.S_IMODE(parent.stat().st_mode) & 0o077:
    raise SystemExit("qualification power-loss output parent is unsafe")
if os.path.lexists(path):
    raise SystemExit("qualification power-loss ready path already exists")
raw = (json.dumps({"schema_version": 1, "qualification_marker": sys.argv[2],
                   "phase": "marker_durable_before_mutation", "pid": int(sys.argv[3])},
                  sort_keys=True, separators=(",", ":")) + "\n").encode()
descriptor, name = tempfile.mkstemp(prefix=".power-loss-ready-", dir=parent)
temporary = Path(name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    os.link(temporary, path, follow_symlinks=False)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)
finally:
    try: temporary.unlink()
    except FileNotFoundError: pass
PY
    kill -STOP "$$"
    die 'qualification power-loss process resumed unexpectedly'
fi

promotion_active=1
docker tag "${candidate_id}" noi-linux-official:2.0
mv -Tf -- "${next_link}" "${current_link}"
next_link_active=0
assert_pair "${new_source}" "${candidate_id}"
rm -f -- "${pending}"
sync -f "${app}"
promotion_active=0
trap - ERR HUP INT TERM
printf 'Imported image promoted: image=%s source=%s rollback_image=%s rollback_source=%s\n' \
    "${candidate_id}" "${new_source}" "${old_image}" "${old_source}"
