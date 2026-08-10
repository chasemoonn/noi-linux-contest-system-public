#!/usr/bin/env bash
set -euo pipefail

contest_ip=${1:?contest IPv4 address required as argument 1}
desktop_token=${2:?desktop token required as argument 2}
: "${EXAM_URL:?set EXAM_URL to the public HTTPS exam origin}"
: "${TEST_SEAT_CONTAINER:?set TEST_SEAT_CONTAINER to a disposable seat container}"
: "${WEB_PROBLEM_SLUG:?set WEB_PROBLEM_SLUG to a disposable web-submit problem}"
: "${FOLDER_PROBLEM_SLUG:?set FOLDER_PROBLEM_SLUG to a disposable folder-submit problem}"
: "${ORCHESTRATOR_ADMIN_FILE:?set ORCHESTRATOR_ADMIN_FILE to a mode-0600 credential file}"
: "${SSH_PRIVATE_KEY:?set SSH_PRIVATE_KEY to the contest host private key}"
: "${SSH_KNOWN_HOSTS:?set SSH_KNOWN_HOSTS to the pinned contest known_hosts file}"
: "${CONFIRM_DESTRUCTIVE_SMOKE_TEST:?set CONFIRM_DESTRUCTIVE_SMOKE_TEST=YES only for an isolated test contest}"

if [[ "${CONFIRM_DESTRUCTIVE_SMOKE_TEST}" != YES ]]; then
  printf 'CONFIRM_DESTRUCTIVE_SMOKE_TEST must equal YES\n' >&2
  exit 2
fi
if [[ ! "${contest_ip}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  printf 'argument 1 must be a literal IPv4 address\n' >&2
  exit 2
fi
IFS=. read -r octet1 octet2 octet3 octet4 <<<"${contest_ip}"
for octet in "${octet1}" "${octet2}" "${octet3}" "${octet4}"; do
  if ((10#${octet} > 255)); then
    printf 'argument 1 must be a valid IPv4 address\n' >&2
    exit 2
  fi
done
if [[ ! "${EXAM_URL}" =~ ^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]+)?$ ]]; then
  printf 'EXAM_URL must be an HTTPS origin without path, query, or credentials\n' >&2
  exit 2
fi
for value_name in desktop_token TEST_SEAT_CONTAINER WEB_PROBLEM_SLUG FOLDER_PROBLEM_SLUG; do
  value=${!value_name}
  if [[ ! "${value}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    printf '%s contains unsupported characters\n' "${value_name}" >&2
    exit 2
  fi
done
for required_file in "${ORCHESTRATOR_ADMIN_FILE}" "${SSH_PRIVATE_KEY}" "${SSH_KNOWN_HOSTS}"; do
  if [[ ! -f "${required_file}" ]]; then
    printf 'required file not found: %s\n' "${required_file}" >&2
    exit 2
  fi
done
if [[ $(stat -c '%a' "${ORCHESTRATOR_ADMIN_FILE}") != 600 ]]; then
  printf 'ORCHESTRATOR_ADMIN_FILE must have mode 0600\n' >&2
  exit 2
fi
ssh_key_mode=$(stat -c '%a' "${SSH_PRIVATE_KEY}")
if [[ "${ssh_key_mode}" != 600 && "${ssh_key_mode}" != 400 ]]; then
  printf 'SSH_PRIVATE_KEY must have mode 0600 or 0400\n' >&2
  exit 2
fi

exam_url=${EXAM_URL%/}
answer_root="/home/student/答案/noi-smoke"
ssh_opts=(
  -i "${SSH_PRIVATE_KEY}"
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile="${SSH_KNOWN_HOSTS}"
)

root_status=$(curl -sS --max-time 12 -o /dev/null -w '%{http_code}' \
  "${exam_url}/s/${desktop_token}/")
page_status=$(curl -sS --max-time 12 -o /dev/null -w '%{http_code}' \
  "${exam_url}/s/${desktop_token}/vnc.html")
unauthorized_admin=$(curl -sS --max-time 12 -o /dev/null -w '%{http_code}' \
  "${exam_url}/admin")

admin_page=$(mktemp)
cleanup() {
  rm -f -- "${admin_page}"
}
trap cleanup EXIT
set -a
# shellcheck disable=SC1090
source "${ORCHESTRATOR_ADMIN_FILE}"
set +a
: "${URL:?credential file must define URL}"
: "${USER:?credential file must define USER}"
: "${PASSWORD:?credential file must define PASSWORD}"
if [[ "${URL%/}" != "${exam_url}/admin" ]]; then
  printf 'credential URL must equal EXAM_URL/admin\n' >&2
  exit 2
fi
authorized_admin=$(curl -sS --max-time 12 -u "${USER}:${PASSWORD}" -o "${admin_page}" \
  -w '%{http_code}' "${URL}")
grep -q '编排后台' "${admin_page}"
case "${root_status}" in
  301|302|307|308) ;;
  *)
    printf 'seat root returned HTTP %s, expected a redirect\n' "${root_status}" >&2
    exit 1
    ;;
esac
if [[ "${page_status}" != 200 ]]; then
  printf 'seat page returned HTTP %s, expected 200\n' "${page_status}" >&2
  exit 1
fi
case "${unauthorized_admin}" in
  401|403) ;;
  *)
    printf 'unauthenticated admin returned HTTP %s, expected 401 or 403\n' \
      "${unauthorized_admin}" >&2
    exit 1
    ;;
esac
if [[ "${authorized_admin}" != 200 ]]; then
  printf 'authenticated admin returned HTTP %s, expected 200\n' \
    "${authorized_admin}" >&2
  exit 1
fi

ssh "${ssh_opts[@]}" "root@${contest_ip}" bash -s -- \
  "${TEST_SEAT_CONTAINER}" "${WEB_PROBLEM_SLUG}" \
  "${FOLDER_PROBLEM_SLUG}" "${answer_root}" <<'REMOTE'
set -euo pipefail
container=$1
web_problem=$2
folder_problem=$3
answer_root=$4
url=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' \
  "${container}" | sed -n 's/^WEB_SUBMIT_URL=//p')
test -n "${url}"
docker exec "${container}" curl -fsS --max-time 10 "${url}" | grep -q '程序回收系统'

first=$(printf '#include <cstdio>\nint main(){freopen("%s.in","r",stdin);freopen("%s.out","w",stdout);return 1;}\n' \
  "${web_problem}" "${web_problem}")
second=$(printf '#include <cstdio>\nint main(){freopen("%s.in","r",stdin);freopen("%s.out","w",stdout);return 2;}\n' \
  "${web_problem}" "${web_problem}")
docker exec "${container}" curl -fsS -L --max-time 10 \
  --data-urlencode "problem=${web_problem}" --data-urlencode "code=${first}" \
  "${url}" >/dev/null
docker exec "${container}" curl -fsS -L --max-time 10 \
  --data-urlencode "problem=${web_problem}" --data-urlencode "code=${second}" \
  "${url}" >/dev/null
docker exec "${container}" curl -fsS --max-time 10 "${url}" | grep -q 'return 2'

folder_code=$(printf '#include <cstdio>\nint main(){freopen("%s.in","r",stdin);freopen("%s.out","w",stdout);return 3;}\n' \
  "${folder_problem}" "${folder_problem}")
encoded=$(printf '%s' "${folder_code}" | base64 -w0)
folder_path="${answer_root}/${folder_problem}/${folder_problem}.cpp"
docker exec "${container}" mkdir -p "$(dirname -- "${folder_path}")"
docker exec "${container}" sh -lc \
  "printf '%s' '${encoded}' | base64 -d > '${folder_path}'"
docker exec "${container}" test -s "${folder_path}"

if docker exec "${container}" curl -m 4 -fsS https://example.com/ >/dev/null 2>&1; then
  printf 'isolated container unexpectedly reached the public internet\n' >&2
  exit 1
fi
network_state=$(docker network inspect seats \
  -f '{{.Internal}} {{index .Options "com.docker.network.bridge.enable_icc"}}')
if [[ "${network_state}" != 'true false' ]]; then
  printf 'seat network isolation mismatch: %s\n' "${network_state}" >&2
  exit 1
fi
REMOTE

printf 'desktop_redirect=%s desktop_page=%s admin_unauthorized=%s admin_authorized=%s\n' \
  "${root_status}" "${page_status}" "${unauthorized_admin}" "${authorized_admin}"
printf 'dual_path_exercised\n'
