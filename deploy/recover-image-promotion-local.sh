#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
    cat <<'EOF'
Usage:
  recover-image-promotion-local.sh --expected-marker-sha256 HEX

Recover an interrupted image/source promotion by converging to the OLD pair
recorded in image-promotion.pending.  The command never completes the NEW
promotion and never starts, stops, or removes a contest seat container.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

expected_marker_sha256=''
original_arguments=("$@")
while (($#)); do
    case "$1" in
        --expected-marker-sha256)
            (($# >= 2)) || die '--expected-marker-sha256 requires a value'
            expected_marker_sha256="$2"
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
[[ "${expected_marker_sha256}" =~ ^[a-f0-9]{64}$ ]] \
    || die '--expected-marker-sha256 must be 64 lowercase hexadecimal characters'
for command_name in docker python3 readlink sync rm mv ln chmod mktemp date; do
    need_cmd "${command_name}"
done

app=${NOI_APP_ROOT:-/opt/noi-linux-contest-system}
pending="${app}/image-promotion.pending"
receipt="${app}/image-promotion-recovery.receipt"
current_link="${app}/current-image-source"
lock_file=/var/lock/noi-official-image-deploy.lock
deployment_lock="${app}/orchestrator/runtime/deploy-image.lock"

# Hold the same inode-verified lock used by imported-image promotion.  Opening
# through O_NOFOLLOW keeps a pre-created symlink/FIFO from becoming a root
# metadata write or an unbounded open.
if [[ "${NOI_IMAGE_RECOVERY_LOCK_HELD:-}" != 1 ]]; then
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
if (
    not stat.S_ISREG(info.st_mode)
    or info.st_uid != 0
    or info.st_gid != 0
    or info.st_nlink != 1
    or stat.S_IMODE(info.st_mode) not in {0o600, 0o644}
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
environment["NOI_IMAGE_RECOVERY_LOCK_HELD"] = "1"
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
    or path_info.st_uid != 0
    or path_info.st_gid != 0
    or path_info.st_nlink != 1
    or stat.S_IMODE(path_info.st_mode) != 0o600
    or (descriptor_info.st_dev, descriptor_info.st_ino)
       != (path_info.st_dev, path_info.st_ino)
):
    raise SystemExit("shared image deployment lock guardian changed")
try:
    fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as exc:
    raise SystemExit(f"shared image deployment lock was lost: {exc}")
PY

# Also exclude the controller's seat-provisioning critical section.  This is a
# separate lock from the host image-deployment lock and is the same path used
# by Pipeline._acquire_deployment_lock().
if [[ "${NOI_IMAGE_RECOVERY_DEPLOYMENT_LOCK_HELD:-}" != 1 ]]; then
    exec python3 - "${deployment_lock}" "$0" "${original_arguments[@]}" <<'PY'
import fcntl
import os
import stat
import sys

lock_path, script, *arguments = sys.argv[1:]
parent, name = os.path.split(lock_path)
if not parent or not name:
    raise SystemExit("controller deployment lock path is invalid")
parent_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
try:
    parent_descriptor = os.open(parent, parent_flags)
except OSError as exc:
    raise SystemExit(f"controller deployment lock parent cannot be opened safely: {exc}")
parent_info = os.fstat(parent_descriptor)
if (
    not stat.S_ISDIR(parent_info.st_mode)
    or parent_info.st_uid != 0
    or parent_info.st_gid != 0
    or stat.S_IMODE(parent_info.st_mode) & 0o022
):
    raise SystemExit("controller deployment lock parent metadata is unsafe")
flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
finally:
    os.close(parent_descriptor)
info = os.fstat(descriptor)
if (
    not stat.S_ISREG(info.st_mode)
    or info.st_uid != 0
    or info.st_gid != 0
    or info.st_nlink != 1
    or stat.S_IMODE(info.st_mode) not in {0o600, 0o644}
):
    raise SystemExit("controller deployment lock metadata is unsafe")
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("contest preparation is running; image recovery is refused")
os.fchmod(descriptor, 0o600)
os.dup2(descriptor, 9, inheritable=True)
os.set_inheritable(9, True)
if descriptor != 9:
    os.close(descriptor)
environment = os.environ.copy()
environment["NOI_IMAGE_RECOVERY_DEPLOYMENT_LOCK_HELD"] = "1"
os.execve("/bin/bash", ["bash", script, *arguments], environment)
PY
fi
python3 - "${deployment_lock}" <<'PY'
import fcntl
import os
import stat
import sys

path = sys.argv[1]
try:
    descriptor_info = os.fstat(9)
    path_info = os.stat(path, follow_symlinks=False)
except OSError as exc:
    raise SystemExit(f"controller deployment lock guardian is absent: {exc}")
if (
    not stat.S_ISREG(path_info.st_mode)
    or path_info.st_uid != 0
    or path_info.st_gid != 0
    or path_info.st_nlink != 1
    or stat.S_IMODE(path_info.st_mode) != 0o600
    or (descriptor_info.st_dev, descriptor_info.st_ino)
       != (path_info.st_dev, path_info.st_ino)
):
    raise SystemExit("controller deployment lock guardian changed")
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as exc:
    raise SystemExit(f"controller deployment lock was lost: {exc}")
PY

python3 - "${app}" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
if not os.path.isabs(path):
    raise SystemExit("NOI_APP_ROOT must be absolute")
info = os.stat(path, follow_symlinks=False)
if (
    not stat.S_ISDIR(info.st_mode)
    or info.st_uid != 0
    or info.st_gid != 0
    or stat.S_IMODE(info.st_mode) & 0o022
):
    raise SystemExit("NOI_APP_ROOT metadata is unsafe")
PY

seat_containers="$(docker ps -q --filter label=noi.contest)" \
    || die 'Docker seat inventory cannot be read'
[[ -z "${seat_containers}" ]] \
    || die 'contest seat containers are running; interrupted promotion recovery is refused'

valid_image_id() {
    [[ "$1" =~ ^sha256:[a-f0-9]{64}$ ]]
}

valid_source_target() {
    [[ "$1" =~ ^image-releases/[A-Za-z0-9TZ-]+$ ]]
}

formal_image_id() {
    local value matches
    if value="$(docker image inspect noi-linux-official:2.0 \
        --format '{{.Id}}' 2>/dev/null)"; then
        valid_image_id "${value}" || return 1
        printf '%s' "${value}"
        return 0
    fi
    # An absent tag is valid for the first managed baseline.  Distinguish it
    # from an unavailable or inconsistent Docker daemon before returning empty.
    docker info >/dev/null 2>&1 || return 1
    matches="$(docker image ls --filter reference=noi-linux-official:2.0 \
        --format '{{.ID}}')" || return 1
    [[ -z "${matches}" ]] || return 1
    printf ''
}

read_metadata_value() {
    python3 - "$1" "$2" <<'PY'
import os
import stat
import sys

path, wanted = sys.argv[1:]
info = os.stat(path, follow_symlinks=False)
if (
    not stat.S_ISREG(info.st_mode)
    or info.st_nlink != 1
    or info.st_uid != 0
    or info.st_gid != 0
    or stat.S_IMODE(info.st_mode) & 0o022
):
    raise SystemExit("promotion metadata is unsafe")
values = {}
with open(path, "r", encoding="utf-8", newline="") as handle:
    for raw in handle:
        line = raw.rstrip("\n")
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise SystemExit("promotion metadata has a duplicate key")
        values[key] = value.rstrip("\r")
if wanted not in values:
    raise SystemExit("promotion metadata misses a required key")
sys.stdout.write(values[wanted])
PY
}

validate_source_directory() {
    python3 - "${app}" "$1" <<'PY'
import os
import stat
import sys

app, target = sys.argv[1:]
if not target.startswith("image-releases/") or "/" in target[len("image-releases/"):]:
    raise SystemExit("release source target is unsafe")
for path in (os.path.join(app, "image-releases"), os.path.join(app, target)):
    info = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise SystemExit("release source directory metadata is unsafe")
PY
}

assert_recorded_pair() {
    local source_target="$1"
    local image_id="$2"
    local metadata recorded_source recorded_image actual_source
    valid_source_target "${source_target}" || return 1
    valid_image_id "${image_id}" || return 1
    validate_source_directory "${source_target}" || return 1
    metadata="${app}/${source_target}/promotion.env"
    recorded_source="$(read_metadata_value "${metadata}" SOURCE_TARGET)" || return 1
    recorded_image="$(read_metadata_value "${metadata}" PROMOTED_IMAGE_ID)" || return 1
    [[ "${recorded_source}" == "${source_target}" \
        && "${recorded_image}" == "${image_id}" ]] || return 1
    [[ "$(docker image inspect "${image_id}" --format '{{.Id}}')" == "${image_id}" ]] \
        || return 1
    if [[ -L "${current_link}" ]]; then
        actual_source="$(readlink "${current_link}")"
        [[ "${actual_source}" =~ ^image-releases/[A-Za-z0-9TZ-]+$ ]] || return 1
    elif [[ -e "${current_link}" ]]; then
        return 1
    fi
}

assert_current_old_state() {
    local current_formal
    current_formal="$(formal_image_id)" || return 1
    if [[ "${old_image_present}" == 1 ]]; then
        [[ "${current_formal}" == "${old_image_id}" \
            && -L "${current_link}" \
            && "$(readlink "${current_link}")" == "${old_source_target}" ]] \
            || return 1
        assert_recorded_pair "${old_source_target}" "${old_image_id}"
    else
        [[ -z "${current_formal}" \
            && ! -e "${current_link}" \
            && ! -L "${current_link}" ]]
    fi
}

parse_record() {
    local record_path="$1"
    local record_kind="$2"
    local output_path="$3"
    python3 - "${record_path}" "${record_kind}" "${output_path}" <<'PY'
import hashlib
import os
import re
import stat
import sys

path, kind, output = sys.argv[1:]
info = os.stat(path, follow_symlinks=False)
if (
    not stat.S_ISREG(info.st_mode)
    or info.st_uid != 0
    or info.st_gid != 0
    or info.st_nlink != 1
    or stat.S_IMODE(info.st_mode) != 0o600
):
    raise SystemExit(f"{kind} metadata is unsafe")
raw = open(path, "rb").read()
if not raw or len(raw) > 8192 or b"\x00" in raw:
    raise SystemExit(f"{kind} bytes are invalid")
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError as exc:
    raise SystemExit(f"{kind} is not UTF-8: {exc}")
values = {}
for line in text.splitlines():
    if not line or "=" not in line:
        raise SystemExit(f"{kind} contains an invalid line")
    key, value = line.split("=", 1)
    if key in values:
        raise SystemExit(f"{kind} has a duplicate key")
    values[key] = value
if kind == "pending marker":
    allowed = {
        "TXN_VERSION", "OLD_IMAGE_PRESENT", "OLD_IMAGE_ID", "OLD_SOURCE_TARGET",
        "NEW_IMAGE_ID", "NEW_SOURCE_TARGET", "NEW_SOURCE_REVISION",
    }
    required = allowed - {"NEW_SOURCE_REVISION"}
    if set(values) not in (required, allowed) or values["TXN_VERSION"] != "1":
        raise SystemExit("pending marker shape differs")
    sha = hashlib.sha256(raw).hexdigest()
    rows = [
        sha, values["OLD_IMAGE_PRESENT"], values["OLD_IMAGE_ID"],
        values["OLD_SOURCE_TARGET"], values["NEW_IMAGE_ID"],
        values["NEW_SOURCE_TARGET"], values.get("NEW_SOURCE_REVISION", ""),
    ]
else:
    required = {
        "RECEIPT_VERSION", "STATUS", "MARKER_SHA256", "OLD_IMAGE_PRESENT",
        "OLD_IMAGE_ID", "OLD_SOURCE_TARGET", "NEW_IMAGE_ID", "NEW_SOURCE_TARGET",
        "RECOVERED_AT",
    }
    if set(values) != required or values["RECEIPT_VERSION"] != "1" \
       or values["STATUS"] != "rolled_back_to_old_pair":
        raise SystemExit("recovery receipt shape differs")
    rows = [
        values["MARKER_SHA256"], values["OLD_IMAGE_PRESENT"],
        values["OLD_IMAGE_ID"], values["OLD_SOURCE_TARGET"],
        values["NEW_IMAGE_ID"], values["NEW_SOURCE_TARGET"], "",
    ]
for value in rows:
    if "\n" in value or "\r" in value:
        raise SystemExit(f"{kind} contains an unsafe value")
with open(output, "w", encoding="utf-8", newline="\n") as handle:
    handle.write("\n".join(rows) + "\n")
os.chmod(output, 0o600)
PY
}

parsed="$(mktemp "${app}/.image-promotion-recovery-parse.XXXXXX")"
cleanup() {
    rm -f -- "${parsed}" "${app}/.restore-image-source-recovery.$$" \
        "${app}/.image-promotion-recovery-receipt.$$"
}
trap cleanup EXIT

if [[ ! -e "${pending}" && ! -L "${pending}" ]]; then
    [[ -e "${receipt}" && ! -L "${receipt}" ]] \
        || die 'no pending image promotion and no matching recovery receipt'
    parse_record "${receipt}" 'recovery receipt' "${parsed}" \
        || die 'recovery receipt validation failed'
    mapfile -t fields < "${parsed}"
    ((${#fields[@]} == 7)) || die 'recovery receipt field count differs'
    marker_sha256="${fields[0]}"
    old_image_present="${fields[1]}"
    old_image_id="${fields[2]}"
    old_source_target="${fields[3]}"
    new_image_id="${fields[4]}"
    new_source_target="${fields[5]}"
    [[ "${marker_sha256}" == "${expected_marker_sha256}" ]] \
        || die 'recovery receipt does not bind the expected marker SHA256'
    assert_current_old_state \
        || die 'the recorded recovery receipt no longer matches current state'
    printf 'image promotion was already recovered: marker_sha256=%s\n' \
        "${marker_sha256}"
    exit 0
fi

parse_record "${pending}" 'pending marker' "${parsed}" \
    || die 'pending marker validation failed'
mapfile -t fields < "${parsed}"
((${#fields[@]} == 7)) || die 'pending marker field count differs'
marker_sha256="${fields[0]}"
old_image_present="${fields[1]}"
old_image_id="${fields[2]}"
old_source_target="${fields[3]}"
new_image_id="${fields[4]}"
new_source_target="${fields[5]}"
new_source_revision="${fields[6]}"

[[ "${marker_sha256}" == "${expected_marker_sha256}" ]] \
    || die 'pending marker SHA256 differs from the operator-confirmed value'
[[ "${old_image_present}" =~ ^[01]$ ]] || die 'OLD_IMAGE_PRESENT is invalid'
valid_image_id "${new_image_id}" || die 'NEW_IMAGE_ID is invalid'
valid_source_target "${new_source_target}" || die 'NEW_SOURCE_TARGET is invalid'
[[ -z "${new_source_revision}" || "${new_source_revision}" =~ ^[a-f0-9]{40}$ ]] \
    || die 'NEW_SOURCE_REVISION is invalid'
assert_recorded_pair "${new_source_target}" "${new_image_id}" \
    || die 'the pending NEW image/source pair is not intact'
if [[ "${old_image_present}" == 1 ]]; then
    valid_image_id "${old_image_id}" || die 'OLD_IMAGE_ID is invalid'
    valid_source_target "${old_source_target}" || die 'OLD_SOURCE_TARGET is invalid'
    assert_recorded_pair "${old_source_target}" "${old_image_id}" \
        || die 'the pending OLD image/source pair is not intact'
else
    [[ -z "${old_image_id}" && -z "${old_source_target}" ]] \
        || die 'an absent OLD baseline must not record an image or source target'
fi

actual_image="$(formal_image_id)" || die 'formal Docker image state cannot be read'
[[ -z "${actual_image}" || "${actual_image}" == "${old_image_id}" \
    || "${actual_image}" == "${new_image_id}" ]] \
    || die 'formal image tag is neither the pending OLD nor NEW image'
if [[ -L "${current_link}" ]]; then
    actual_source="$(readlink "${current_link}")"
    [[ "${actual_source}" == "${old_source_target}" \
        || "${actual_source}" == "${new_source_target}" ]] \
        || die 'current source link is neither the pending OLD nor NEW source'
elif [[ -e "${current_link}" ]]; then
    die 'current-image-source exists but is not a symlink'
fi

# Recovery is intentionally one-way: converge to OLD.  The persistent pending
# marker remains in place across every mutation, so SIGKILL can only require the
# same idempotent recovery command again.
if [[ "${old_image_present}" == 1 ]]; then
    docker tag "${old_image_id}" noi-linux-official:2.0
    restore_link="${app}/.restore-image-source-recovery.$$"
    rm -f -- "${restore_link}"
    ln -s "${old_source_target}" "${restore_link}"
    mv -Tf -- "${restore_link}" "${current_link}"
else
    current_formal="$(formal_image_id)" \
        || die 'formal Docker image state cannot be read before recovery'
    if [[ -n "${current_formal}" ]]; then
        docker image rm noi-linux-official:2.0 >/dev/null
    fi
    rm -f -- "${current_link}"
fi
sync -f "${app}"
assert_current_old_state || die 'recovered OLD image/source state did not verify'

receipt_temp="${app}/.image-promotion-recovery-receipt.$$"
{
    printf 'RECEIPT_VERSION=1\n'
    printf 'STATUS=rolled_back_to_old_pair\n'
    printf 'MARKER_SHA256=%s\n' "${marker_sha256}"
    printf 'OLD_IMAGE_PRESENT=%s\n' "${old_image_present}"
    printf 'OLD_IMAGE_ID=%s\n' "${old_image_id}"
    printf 'OLD_SOURCE_TARGET=%s\n' "${old_source_target}"
    printf 'NEW_IMAGE_ID=%s\n' "${new_image_id}"
    printf 'NEW_SOURCE_TARGET=%s\n' "${new_source_target}"
    printf 'RECOVERED_AT=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${receipt_temp}"
chmod 0600 "${receipt_temp}"
sync -f "${receipt_temp}"
mv -Tf -- "${receipt_temp}" "${receipt}"
sync -f "${app}"
rm -f -- "${pending}"
sync -f "${app}"

printf 'interrupted image promotion recovered to OLD pair: marker_sha256=%s\n' \
    "${marker_sha256}"
