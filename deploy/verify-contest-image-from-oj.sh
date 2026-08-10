#!/bin/bash
set -euo pipefail

contest_ip="${1:?contest IP required}"
app=/opt/noi-linux-contest-system
install -d -m 0755 "${app}/orchestrator/runtime"
exec 9>"${app}/orchestrator/runtime/deploy-image.lock"
if ! flock -n 9; then
  echo "image deployment, verification, or contest preparation is running" >&2
  exit 1
fi
ssh_opts=(
  -i "${app}/secrets/contest.pem"
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile="${app}/secrets/known_hosts"
)

ssh "${ssh_opts[@]}" "root@${contest_ip}" 'bash -s' <<'REMOTE'
set -euo pipefail
exec 8>/var/lock/noi-official-image-deploy.lock
if ! flock -n 8; then
  echo "another image deployment or verification is already running" >&2
  exit 1
fi
if [[ -n "$(docker ps -q --filter label=noi.contest)" ]]; then
  echo "contest seat containers are running; image verification is refused" >&2
  exit 1
fi
app=/opt/noi-linux-contest-system
current_link="${app}/current-image-source"
pending_transaction="${app}/image-promotion.pending"
if [[ -e "${pending_transaction}" || -L "${pending_transaction}" ]]; then
  echo "an interrupted image promotion must be recovered before verification" >&2
  exit 1
fi
if [[ ! -L "${current_link}" ]]; then
  echo "current-image-source is not a managed release symlink" >&2
  exit 1
fi
source_target="$(readlink "${current_link}")"
if [[ ! "${source_target}" =~ ^image-releases/[A-Za-z0-9TZ-]+$ \
  || ! -d "${app}/${source_target}" ]]; then
  echo "current-image-source target is unsafe or missing" >&2
  exit 1
fi
metadata="${app}/${source_target}/promotion.env"
test -r "${metadata}"
read_value() {
  awk -F= -v wanted="$1" \
    '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "${metadata}"
}
recorded_source="$(read_value SOURCE_TARGET)"
promoted_image_id="$(read_value PROMOTED_IMAGE_ID)"
promoted_source_revision="$(read_value SOURCE_REVISION)"
formal_image_id="$(docker image inspect noi-linux-official:2.0 --format '{{.Id}}')"
if [[ "${recorded_source}" != "${source_target}" \
  || ! "${promoted_image_id}" =~ ^sha256:[0-9a-f]{64}$ \
  || ! "${promoted_source_revision}" =~ ^[0-9a-f]{40}$ \
  || "${formal_image_id}" != "${promoted_image_id}" ]]; then
  echo "formal image tag, source revision, and current release metadata are inconsistent" >&2
  exit 1
fi
source_root="${app}/${source_target}"
cd "${source_root}"
test -f deploy/verify-contest-image-local.sh
bash deploy/verify-contest-image-local.sh \
  "${promoted_image_id}" "${promoted_source_revision}"
if [[ "$(docker image inspect noi-linux-official:2.0 --format '{{.Id}}')" \
  != "${promoted_image_id}" || "$(readlink "${current_link}")" != "${source_target}" ]]; then
  echo "formal image/source pair changed during verification" >&2
  exit 1
fi
REMOTE
