#!/bin/bash
set -euo pipefail

contest_ip="${1:?contest IP required}"
app=/opt/noi-linux-contest-system
install -d -m 0755 "${app}/orchestrator/runtime"
exec 9>"${app}/orchestrator/runtime/deploy-image.lock"
if ! flock -n 9; then
  echo "image deployment, verification, rollback, or contest preparation is running" >&2
  exit 1
fi

ssh_opts=(
  -i "${app}/secrets/contest.pem"
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile="${app}/secrets/known_hosts"
)
ssh "${ssh_opts[@]}" "root@${contest_ip}" \
  'test -r /opt/noi-linux-contest-system/current-image-source/deploy/rollback-contest-image-local.sh && exec bash /opt/noi-linux-contest-system/current-image-source/deploy/rollback-contest-image-local.sh'
