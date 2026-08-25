#!/bin/bash
set -euo pipefail

contest_ip="${1:?contest IP required}"
remote_iso="${2:-/opt/noi-linux-contest-system/ubuntu-noi-v2.0.iso}"
app=/opt/noi-linux-contest-system
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source_root="$(cd -- "${script_dir}/.." && pwd -P)"
key="${app}/secrets/contest.pem"
known="${app}/secrets/known_hosts"
expected="${CONTEST_SSH_HOST_KEY_SHA256:?set CONTEST_SSH_HOST_KEY_SHA256}"
source_revision="${NOI_SOURCE_REVISION:?set NOI_SOURCE_REVISION to the exact 40-character lowercase source revision}"
if [[ ! "${source_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "NOI_SOURCE_REVISION must be exactly 40 lowercase hexadecimal characters" >&2
  exit 2
fi
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c1-12)"
remote_stage="${app}/.image-staging/${release_id}"
scan="$(mktemp /tmp/noi-contest-key.XXXXXX)"
trap 'rm -f "${scan}"' EXIT

test -d "${source_root}/noi-linux-official"
test -f "${source_root}/deploy/build-noi-official-image.sh"
test -f "${source_root}/deploy/rollback-contest-image-local.sh"
test -f "${source_root}/deploy/verify-contest-image-local.sh"
if find "${source_root}" -xdev -perm /022 -print -quit | grep -q .; then
  echo "candidate release source contains group/other-writable paths" >&2
  exit 1
fi
for source_tree in "${source_root}/noi-linux-official" "${source_root}/deploy"; do
  if find "${source_tree}" -xdev -type d ! -perm -0555 -print -quit \
      | grep -q . \
    || find "${source_tree}" -xdev -type f ! -perm -0444 -print -quit \
      | grep -q .; then
    echo "candidate release source contains unreadable paths" >&2
    exit 1
  fi
done

install -d -m 0755 "${app}/orchestrator/runtime"
exec 9>"${app}/orchestrator/runtime/deploy-image.lock"
if ! flock -n 9; then
  echo "another image deployment is already running on the OJ host" >&2
  exit 1
fi

if [[ ! "${remote_iso}" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
  echo "remote ISO path contains unsafe characters" >&2
  exit 2
fi

ssh-keyscan -T 10 -t ed25519 "${contest_ip}" > "${scan}" 2>/dev/null
actual="$(ssh-keygen -lf "${scan}" -E sha256 | awk '{print $2}')"
if [[ "${actual}" != "${expected}" ]]; then
  echo "contest host key mismatch: ${actual}" >&2
  exit 1
fi
install -m 0644 "${scan}" "${known}"

ssh_opts=(
  -i "${key}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile="${known}"
)

tar czf - -C "${source_root}" \
  noi-linux-official \
  deploy/build-noi-official-image.sh \
  deploy/rollback-contest-image-local.sh \
  deploy/verify-contest-image-local.sh \
  | ssh "${ssh_opts[@]}" "root@${contest_ip}" \
      "stage='${remote_stage}'; test ! -e \"\${stage}\" && install -d -m 0755 \"\${stage}\" && { tar xzf - -C \"\${stage}\" || { rm -rf -- \"\${stage}\"; exit 1; }; }"

ssh "${ssh_opts[@]}" "root@${contest_ip}" \
  "bash -s -- '${remote_iso}' '${remote_stage}' '${release_id}' '${source_revision}'" <<'REMOTE'
set -euo pipefail
iso_path="${1}"
stage="${2}"
release_id="${3}"
source_revision="${4}"
app=/opt/noi-linux-contest-system
expected_iso_sha256='c8824240736352e5e4aaf3f6532b40961f75fa9f23d670bb78881355a49d5878'
candidate="noi-linux-official:candidate-${release_id}"
rollback="noi-linux-official:rollback-${release_id}"
current_link="${app}/current-image-source"
pending_transaction="${app}/image-promotion.pending"

if [[ ! "${source_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "source revision must be exactly 40 lowercase hexadecimal characters" >&2
  exit 2
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

complete_baseline_source() {
  local root="$1"
  [[ -d "${root}/noi-linux-official" ]] \
    && [[ -f "${root}/deploy/build-noi-official-image.sh" ]] \
    && [[ -f "${root}/deploy/verify-contest-image-local.sh" ]]
}

assert_current_pair() {
  local expected_source="${1:-}"
  local expected_image="${2:-}"
  local source metadata recorded_source recorded_image actual_image
  if [[ ! -L "${current_link}" ]]; then
    echo "current-image-source is not a managed release symlink" >&2
    return 1
  fi
  source="$(readlink "${current_link}")"
  if ! valid_source_target "${source}" || [[ ! -d "${app}/${source}" ]]; then
    echo "current-image-source target is unsafe or missing" >&2
    return 1
  fi
  metadata="${app}/${source}/promotion.env"
  if [[ ! -r "${metadata}" ]]; then
    echo "current release has no readable promotion.env" >&2
    return 1
  fi
  recorded_source="$(read_value "${metadata}" SOURCE_TARGET)"
  recorded_image="$(read_value "${metadata}" PROMOTED_IMAGE_ID)"
  actual_image="$(formal_image_id)"
  if [[ "${recorded_source}" != "${source}" ]] \
    || ! valid_image_id "${recorded_image}" \
    || [[ "${actual_image}" != "${recorded_image}" ]]; then
    echo "formal image tag and current release metadata are inconsistent" >&2
    return 1
  fi
  if [[ -n "${expected_source}" && "${source}" != "${expected_source}" ]]; then
    echo "current source is not the expected release" >&2
    return 1
  fi
  if [[ -n "${expected_image}" && "${actual_image}" != "${expected_image}" ]]; then
    echo "formal image is not the expected immutable image ID" >&2
    return 1
  fi
}

seed_existing_baseline() {
  local image_id="$1"
  local baseline_id="baseline-${release_id}"
  local baseline_target="image-releases/${baseline_id}"
  local baseline_root="${app}/${baseline_target}"
  local baseline_stage="${app}/.baseline-source-${release_id}"
  local baseline_source_root="${app}"
  local baseline_source="existing-app"
  if ! complete_baseline_source "${baseline_source_root}"; then
    if ! complete_baseline_source "${stage}"; then
      echo "cannot seed first managed baseline: existing app and current stage both lack a complete required source set" >&2
      return 1
    fi
    baseline_source_root="${stage}"
    baseline_source="current-stage-fallback"
  fi
  test ! -e "${baseline_root}"
  test ! -e "${baseline_stage}"
  install -d -m 0755 "${baseline_stage}/deploy"
  cp -a -- "${baseline_source_root}/noi-linux-official" "${baseline_stage}/"
  cp -a -- "${baseline_source_root}/deploy/build-noi-official-image.sh" \
    "${baseline_source_root}/deploy/verify-contest-image-local.sh" \
    "${baseline_stage}/deploy/"
  if [[ -f "${baseline_source_root}/deploy/rollback-contest-image-local.sh" ]]; then
    cp -a -- "${baseline_source_root}/deploy/rollback-contest-image-local.sh" \
      "${baseline_stage}/deploy/"
  fi
  {
    printf 'PROMOTED_IMAGE_ID=%s\n' "${image_id}"
    printf 'SOURCE_TARGET=%s\n' "${baseline_target}"
    printf 'BASELINE_UNVERIFIED=1\n'
    printf 'BASELINE_SOURCE=%s\n' "${baseline_source}"
    printf 'ROLLBACK_TAG=\n'
    printf 'ROLLBACK_IMAGE_ID=\n'
    printf 'ROLLBACK_SOURCE_TARGET=\n'
  } > "${baseline_stage}/promotion.env"
  mv -- "${baseline_stage}" "${baseline_root}"
  baseline_link="${app}/.baseline-image-source-${release_id}"
  ln -s "${baseline_target}" "${baseline_link}"
  mv -Tf -- "${baseline_link}" "${current_link}"
  assert_current_pair "${baseline_target}" "${image_id}"
  echo "seeded managed baseline ${baseline_target} for existing formal image (source=${baseline_source})"
}

if [[ ! "${stage}" =~ ^/opt/noi-linux-contest-system/\.image-staging/[A-Za-z0-9TZ-]+$ \
  || ! "${release_id}" =~ ^[A-Za-z0-9TZ-]+$ ]]; then
  echo "invalid deployment staging path" >&2
  exit 2
fi
stage_active=1
cleanup_stage() {
  if [[ "${stage_active}" == "1" ]]; then
    rm -rf -- "${stage}"
  fi
}
trap cleanup_stage EXIT
exec 8>/var/lock/noi-official-image-deploy.lock
if ! flock -n 8; then
  echo "another image deployment is already running on the contest host" >&2
  exit 1
fi
if [[ -n "$(docker ps -q --filter label=noi.contest)" ]]; then
  echo "contest seat containers are running; image deployment is refused" >&2
  exit 1
fi

install -d -m 0755 "${app}/image-releases"
if [[ -e "${pending_transaction}" || -L "${pending_transaction}" ]]; then
  echo "unfinished image transaction found at ${pending_transaction}; explicit recovery is required" >&2
  exit 1
fi
if [[ -L "${current_link}" ]]; then
  assert_current_pair
elif [[ -e "${current_link}" ]]; then
  echo "current-image-source exists but is not a symlink" >&2
  exit 1
else
  existing_image_id="$(formal_image_id)"
  if [[ -n "${existing_image_id}" ]]; then
    if ! valid_image_id "${existing_image_id}"; then
      echo "current official image has an invalid image ID" >&2
      exit 1
    fi
    seed_existing_baseline "${existing_image_id}"
  fi
fi

cd "${stage}"
bash -n noi-linux-official/rootfs/usr/local/bin/contest-entrypoint.sh
bash -n noi-linux-official/rootfs/usr/local/bin/start-contest-vnc.sh
bash -n deploy/rollback-contest-image-local.sh
bash -n deploy/verify-contest-image-local.sh

rootfs_sha256="$(docker image inspect noi-linux-official-rootfs:2.0 \
  --format '{{index .Config.Labels "org.noi.iso.sha256"}}' 2>/dev/null || true)"
rootfs_sha256="${rootfs_sha256,,}"
if [[ "${rootfs_sha256}" == "${expected_iso_sha256}" ]]; then
  docker build --build-arg NOI_ROOTFS_IMAGE=noi-linux-official-rootfs:2.0 \
    --build-arg APT_MIRROR=mirrors.cloud.aliyuncs.com \
    --build-arg "NOI_SOURCE_REVISION=${source_revision}" \
    --build-arg "NOI_ISO_SHA256=${expected_iso_sha256}" \
    -t "${candidate}" noi-linux-official
else
  echo "缓存 rootfs 缺少匹配的官方 ISO 标签，重新从 ISO 构建候选镜像"
  test -f "${iso_path}"
  bash deploy/build-noi-official-image.sh \
    "${iso_path}" "${candidate}" "${source_revision}"
fi
candidate_image_id="$(docker image inspect "${candidate}" --format '{{.Id}}')"
if [[ ! "${candidate_image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "candidate image has an invalid image ID" >&2
  exit 1
fi
candidate_source_revision="$(docker image inspect "${candidate_image_id}" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
if [[ "${candidate_source_revision}" != "${source_revision}" ]]; then
  echo "candidate image source revision does not match the invoked release source" >&2
  exit 1
fi
bash deploy/verify-contest-image-local.sh \
  "${candidate_image_id}" "${source_revision}"
if [[ "$(docker image inspect "${candidate}" --format '{{.Id}}')" \
  != "${candidate_image_id}" ]]; then
  echo "candidate tag changed during verification" >&2
  exit 1
fi

release_root="${app}/image-releases/${release_id}"
new_source_target="image-releases/${release_id}"
test ! -e "${release_root}"
cd "${app}"
mv -- "${stage}" "${release_root}"
stage_active=0
next_link="${app}/.current-image-source-${release_id}"
ln -s "${new_source_target}" "${next_link}"

if [[ -n "$(docker ps -q --filter label=noi.contest)" ]]; then
  echo "contest seats started during candidate verification; promotion is refused" >&2
  rm -f -- "${next_link}"
  exit 1
fi
old_image_present=0
current_image_id="$(formal_image_id)"
old_source_target=""
if [[ -L "${current_link}" ]]; then
  assert_current_pair
  old_image_present=1
  old_source_target="$(readlink "${current_link}")"
elif [[ -e "${current_link}" || -n "${current_image_id}" ]]; then
  echo "formal image and current source lost their managed pairing" >&2
  exit 1
fi
rollback_tag=""
if [[ "${old_image_present}" == "1" ]]; then
  if ! valid_image_id "${current_image_id}"; then
    echo "current official image has an invalid image ID" >&2
    exit 1
  fi
  docker tag "${current_image_id}" "${rollback}"
  rollback_tag="${rollback}"
  echo "旧正式镜像已保留为 ${rollback}"
fi

{
  printf 'PROMOTED_IMAGE_ID=%s\n' "${candidate_image_id}"
  printf 'SOURCE_TARGET=%s\n' "${new_source_target}"
  printf 'SOURCE_REVISION=%s\n' "${source_revision}"
  printf 'ROLLBACK_TAG=%s\n' "${rollback_tag}"
  printf 'ROLLBACK_IMAGE_ID=%s\n' "${current_image_id}"
  printf 'ROLLBACK_SOURCE_TARGET=%s\n' "${old_source_target}"
} > "${release_root}/promotion.env"

promotion_active=0
rollback_promotion() {
  local rc="${1:-$?}"
  local restore_failed=0
  trap - ERR HUP INT TERM
  set +e
  if [[ "${promotion_active}" == "1" ]]; then
    if [[ "${old_image_present}" == "1" ]]; then
      docker tag "${current_image_id}" noi-linux-official:2.0 \
        || restore_failed=1
    else
      if docker image inspect noi-linux-official:2.0 >/dev/null 2>&1; then
        docker image rm noi-linux-official:2.0 >/dev/null 2>&1 \
          || restore_failed=1
      fi
    fi
    if [[ -n "${old_source_target}" ]]; then
      restore_link="${app}/.restore-image-source-${release_id}"
      rm -f -- "${restore_link}"
      ln -s "${old_source_target}" "${restore_link}" \
        || restore_failed=1
      if [[ "${restore_failed}" == "0" ]]; then
        mv -Tf -- "${restore_link}" "${current_link}" \
          || restore_failed=1
      fi
    else
      rm -f -- "${current_link}" || restore_failed=1
    fi
    if [[ "${restore_failed}" == "0" ]]; then
      if [[ "${old_image_present}" == "1" ]]; then
        assert_current_pair "${old_source_target}" "${current_image_id}" \
          || restore_failed=1
      elif [[ -L "${current_link}" || -e "${current_link}" \
        || -n "$(formal_image_id)" ]]; then
        restore_failed=1
      fi
    fi
    if [[ "${restore_failed}" == "0" ]]; then
      rm -f -- "${pending_transaction}"
      sync -f "${app}" || true
    else
      echo "promotion rollback was incomplete; transaction marker retained" >&2
    fi
  fi
  exit "${rc}"
}
trap 'rollback_promotion $?' ERR
trap 'rollback_promotion 129' HUP
trap 'rollback_promotion 130' INT
trap 'rollback_promotion 143' TERM

promotion_active=1
transaction_temp="${pending_transaction}.${release_id}"
rm -f -- "${transaction_temp}"
{
  printf 'TXN_VERSION=1\n'
  printf 'OLD_IMAGE_PRESENT=%s\n' "${old_image_present}"
  printf 'OLD_IMAGE_ID=%s\n' "${current_image_id}"
  printf 'OLD_SOURCE_TARGET=%s\n' "${old_source_target}"
  printf 'NEW_IMAGE_ID=%s\n' "${candidate_image_id}"
  printf 'NEW_SOURCE_TARGET=%s\n' "${new_source_target}"
  printf 'NEW_SOURCE_REVISION=%s\n' "${source_revision}"
} > "${transaction_temp}"
chmod 0600 "${transaction_temp}"
sync -f "${transaction_temp}"
mv -Tf -- "${transaction_temp}" "${pending_transaction}"
sync -f "${app}"

docker tag "${candidate_image_id}" noi-linux-official:2.0
mv -Tf -- "${next_link}" "${current_link}"
assert_current_pair "${new_source_target}" "${candidate_image_id}"
rm -f -- "${pending_transaction}"
sync -f "${app}"
promotion_active=0
trap - ERR HUP INT TERM
echo "候选镜像验收通过并已提升：${candidate} -> noi-linux-official:2.0"
REMOTE
