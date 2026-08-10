#!/usr/bin/env bash
set -euo pipefail

app=${NOI_APP_ROOT:-/opt/noi-linux-contest-system}
current_link="${app}/current-image-source"
pending_transaction="${app}/image-promotion.pending"

exec 8>/var/lock/noi-official-image-deploy.lock
if ! flock -n 8; then
    echo "another image deployment, verification, or rollback is running" >&2
    exit 1
fi
if [[ -n "$(docker ps -q --filter label=noi.contest)" ]]; then
    echo "contest seat containers are running; rollback is refused" >&2
    exit 1
fi

read_value() {
    local file="$1"
    local key="$2"
    awk -F= -v wanted="${key}" \
        '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "${file}"
}

valid_image_id() {
    [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]
}

valid_source_target() {
    [[ "$1" =~ ^image-releases/[A-Za-z0-9TZ-]+$ ]]
}

formal_image_id() {
    docker image inspect noi-linux-official:2.0 \
        --format '{{.Id}}' 2>/dev/null || true
}

assert_pair() {
    local expected_source="$1"
    local expected_image="$2"
    local metadata recorded_source recorded_image actual_image
    if ! valid_source_target "${expected_source}" \
        || [[ ! -d "${app}/${expected_source}" ]]; then
        echo "release source target is unsafe or missing" >&2
        return 1
    fi
    metadata="${app}/${expected_source}/promotion.env"
    if [[ ! -r "${metadata}" ]]; then
        echo "release has no readable promotion.env" >&2
        return 1
    fi
    recorded_source="$(read_value "${metadata}" SOURCE_TARGET)"
    recorded_image="$(read_value "${metadata}" PROMOTED_IMAGE_ID)"
    actual_image="$(formal_image_id)"
    if [[ "${recorded_source}" != "${expected_source}" \
        || "${recorded_image}" != "${expected_image}" \
        || "${actual_image}" != "${expected_image}" ]]; then
        echo "formal image tag and release metadata are inconsistent" >&2
        return 1
    fi
}

if [[ -e "${pending_transaction}" || -L "${pending_transaction}" ]]; then
    echo "unfinished image transaction found at ${pending_transaction}; explicit recovery is required" >&2
    exit 1
fi

if [[ ! -L "${current_link}" ]]; then
    echo "current-image-source is not a release symlink" >&2
    exit 1
fi
current_source="$(readlink "${current_link}")"
if ! valid_source_target "${current_source}"; then
    echo "current source target is unsafe" >&2
    exit 1
fi
metadata="${app}/${current_source}/promotion.env"
test -r "${metadata}"
current_image_id="$(read_value "${metadata}" PROMOTED_IMAGE_ID)"
if ! valid_image_id "${current_image_id}"; then
    echo "current release has an invalid promoted image ID" >&2
    exit 1
fi
assert_pair "${current_source}" "${current_image_id}"

rollback_tag="$(read_value "${metadata}" ROLLBACK_TAG)"
rollback_image_id="$(read_value "${metadata}" ROLLBACK_IMAGE_ID)"
rollback_source="$(read_value "${metadata}" ROLLBACK_SOURCE_TARGET)"
if [[ ! "${rollback_tag}" =~ ^noi-linux-official:rollback-[A-Za-z0-9TZ-]+$ \
    || ! "${rollback_image_id}" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || ! valid_source_target "${rollback_source}"; then
    echo "this release has no valid paired rollback" >&2
    exit 1
fi
if [[ "$(docker image inspect "${rollback_tag}" --format '{{.Id}}')" \
    != "${rollback_image_id}" ]]; then
    echo "rollback tag no longer matches recorded image ID" >&2
    exit 1
fi
rollback_metadata="${app}/${rollback_source}/promotion.env"
if [[ ! -r "${rollback_metadata}" \
    || "$(read_value "${rollback_metadata}" SOURCE_TARGET)" != "${rollback_source}" \
    || "$(read_value "${rollback_metadata}" PROMOTED_IMAGE_ID)" != "${rollback_image_id}" ]]; then
    echo "rollback image and source snapshot are not a recorded pair" >&2
    exit 1
fi

next_link="${app}/.rollback-image-source-$$"
ln -s "${rollback_source}" "${next_link}"
rollback_active=0
restore_current() {
    local rc="${1:-$?}"
    local restore_failed=0
    trap - ERR HUP INT TERM
    set +e
    if [[ "${rollback_active}" == "1" ]]; then
        docker tag "${current_image_id}" noi-linux-official:2.0 \
            || restore_failed=1
        restore_link="${app}/.restore-image-source-$$"
        rm -f -- "${restore_link}"
        ln -s "${current_source}" "${restore_link}" \
            || restore_failed=1
        if [[ "${restore_failed}" == "0" ]]; then
            mv -Tf -- "${restore_link}" "${current_link}" \
                || restore_failed=1
            assert_pair "${current_source}" "${current_image_id}" \
                || restore_failed=1
        fi
        rm -f -- "${next_link}"
        if [[ "${restore_failed}" == "0" ]]; then
            rm -f -- "${pending_transaction}"
            sync -f "${app}" || true
        else
            echo "rollback recovery was incomplete; transaction marker retained" >&2
        fi
    fi
    exit "${rc}"
}
trap 'restore_current $?' ERR
trap 'restore_current 129' HUP
trap 'restore_current 130' INT
trap 'restore_current 143' TERM

rollback_active=1
transaction_temp="${pending_transaction}.rollback-$$"
rm -f -- "${transaction_temp}"
{
    printf 'TXN_VERSION=1\n'
    printf 'OLD_IMAGE_PRESENT=1\n'
    printf 'OLD_IMAGE_ID=%s\n' "${current_image_id}"
    printf 'OLD_SOURCE_TARGET=%s\n' "${current_source}"
    printf 'NEW_IMAGE_ID=%s\n' "${rollback_image_id}"
    printf 'NEW_SOURCE_TARGET=%s\n' "${rollback_source}"
} > "${transaction_temp}"
chmod 0600 "${transaction_temp}"
sync -f "${transaction_temp}"
mv -Tf -- "${transaction_temp}" "${pending_transaction}"
sync -f "${app}"

docker tag "${rollback_image_id}" noi-linux-official:2.0
mv -Tf -- "${next_link}" "${current_link}"
assert_pair "${rollback_source}" "${rollback_image_id}"
rm -f -- "${pending_transaction}"
sync -f "${app}"
rollback_active=0
trap - ERR HUP INT TERM
echo "paired rollback complete: image=${rollback_image_id} source=${rollback_source}"
