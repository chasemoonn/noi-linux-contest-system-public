#!/usr/bin/env bash
set -uo pipefail

: "${EXAM_URL:?set EXAM_URL to the public HTTPS teacher/exam origin}"
: "${HYDRO_URL:?set HYDRO_URL to the public HTTPS Hydro origin}"
: "${EXPECTED_OJ_CIDR:?set EXPECTED_OJ_CIDR to the exact OJ host IPv4 /32}"
for origin_name in EXAM_URL HYDRO_URL; do
  origin=${!origin_name}
  if [[ ! "${origin}" =~ ^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]+)?$ ]]; then
    printf '%s must be an HTTPS origin without path, query, or credentials\n' \
      "${origin_name}" >&2
    exit 2
  fi
done

validate_ipv4_cidr() {
  local value=$1
  local address prefix octet octet1 octet2 octet3 octet4
  [[ "${value}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/([0-9]|[12][0-9]|3[0-2])$ ]] || return 1
  address=${value%/*}
  prefix=${value##*/}
  IFS=. read -r octet1 octet2 octet3 octet4 <<<"${address}"
  for octet in "${octet1}" "${octet2}" "${octet3}" "${octet4}"; do
    ((10#${octet} <= 255)) || return 1
  done
  [[ "${prefix}" =~ ^([0-9]|[12][0-9]|3[0-2])$ ]]
}

if ! validate_ipv4_cidr "${EXPECTED_OJ_CIDR}" ||
    [[ "${EXPECTED_OJ_CIDR##*/}" != 32 ]]; then
  printf 'EXPECTED_OJ_CIDR must be one valid IPv4 address with /32\n' >&2
  exit 2
fi
DEPLOYMENT_LABEL=${DEPLOYMENT_LABEL:-custom}
DESKTOP_ACCESS_MODE=${DESKTOP_ACCESS_MODE:-proxy}
STUDENT_CIDRS=${STUDENT_CIDRS:-}
DESKTOP_PROBE_TOKEN=${DESKTOP_PROBE_TOKEN:-}
DESKTOP_PROBE_QUALITY=${DESKTOP_PROBE_QUALITY:-9}
DESKTOP_PROBE_COMPRESSION=${DESKTOP_PROBE_COMPRESSION:-2}
case "${DESKTOP_ACCESS_MODE}" in
  proxy)
    DESKTOP_PROBE_BASE_URL=${DESKTOP_PROBE_BASE_URL:-${EXAM_URL}}
    ;;
  direct)
    : "${STUDENT_CIDRS:?direct mode requires STUDENT_CIDRS}"
    : "${DESKTOP_PROBE_BASE_URL:?direct mode requires DESKTOP_PROBE_BASE_URL}"
    if [[ "${STUDENT_CIDRS}" == *,* ]]; then
      printf 'direct mode supports exactly one STUDENT_CIDRS value\n' >&2
      exit 2
    fi
    if ! validate_ipv4_cidr "${STUDENT_CIDRS}"; then
      printf 'STUDENT_CIDRS must be one valid IPv4 CIDR\n' >&2
      exit 2
    fi
    if [[ "${STUDENT_CIDRS}" == 0.0.0.0/0 ]] &&
        [[ "${CONFIRM_PUBLIC_DESKTOP_CIDR:-}" != YES ]]; then
      printf '%s\n' \
        'STUDENT_CIDRS=0.0.0.0/0 requires CONFIRM_PUBLIC_DESKTOP_CIDR=YES' >&2
      exit 2
    fi
    ;;
  *)
    printf 'unknown DESKTOP_ACCESS_MODE: %s\n' "${DESKTOP_ACCESS_MODE}" >&2
    exit 2
    ;;
esac
ORCHESTRATOR_CONTAINER=${ORCHESTRATOR_CONTAINER:-noi-orchestrator}
PROJECT_ROOT=${PROJECT_ROOT:-/opt/noi-linux-contest-system}
CADDYFILE=${CADDYFILE:-/root/.hydro/Caddyfile}
CADDY_BIN=${CADDY_BIN:-/root/.nix-profile/bin/caddy}
CADDY_ADMIN_URL=${CADDY_ADMIN_URL:-http://127.0.0.1:2019}

pass_count=0
warn_count=0
fail_count=0

pass() {
  pass_count=$((pass_count + 1))
  printf '[PASS] %s\n' "$*"
}

warn() {
  warn_count=$((warn_count + 1))
  printf '[WARN] %s\n' "$*"
}

fail() {
  fail_count=$((fail_count + 1))
  printf '[FAIL] %s\n' "$*" >&2
}

http_status() {
  local code
  code=$(curl --silent --show-error --output /dev/null --max-time 12 \
    --write-out '%{http_code}' "$1" 2>/dev/null || true)
  printf '%s' "${code:-000}"
}

websocket_status() {
  local code
  code=$(curl --silent --show-error --output /dev/null --max-time 12 \
    --http1.1 --write-out '%{http_code}' \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Version: 13' \
    -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    -H 'Sec-WebSocket-Protocol: binary' "$1" 2>/dev/null || true)
  printf '%s' "${code:-000}"
}

check_mode_600() {
  local path=$1
  local mode
  if [[ ! -f "${path}" ]]; then
    fail "missing sensitive file: ${path}"
    return
  fi
  mode=$(stat -c '%a' "${path}" 2>/dev/null || true)
  if [[ "${mode}" == '600' ]]; then
    pass "mode 600: ${path}"
  else
    fail "expected mode 600 but found ${mode:-unknown}: ${path}"
  fi
}

check_plugin_state_file() {
  local path=$1
  local mode
  if [[ -z "${path}" ]]; then
    fail 'plugin state path is empty'
    return
  fi
  if [[ ! -f "${path}" ]]; then
    fail "missing plugin state file: ${path}"
    return
  fi
  mode=$(stat -c '%a' "${path}" 2>/dev/null || true)
  if [[ "${mode}" != '600' ]]; then
    fail "expected plugin state mode 600 but found ${mode:-unknown}: ${path}"
    return
  fi
  if [[ ! -w "${path}" ]]; then
    fail "plugin state file is not writable: ${path}"
    return
  fi
  pass "plugin state file ready: ${path}"
}

printf 'NOI Linux read-only health check (%s)\n' "${DEPLOYMENT_LABEL}"
printf 'exam=%s hydro=%s expected_oj=%s desktop_mode=%s desktop_probe=%s\n\n' \
  "${EXAM_URL}" "${HYDRO_URL}" "${EXPECTED_OJ_CIDR}" \
  "${DESKTOP_ACCESS_MODE}" "${DESKTOP_PROBE_BASE_URL}"

if [[ ${EUID} -eq 0 ]]; then
  pass 'running as root'
else
  warn 'not running as root; some local checks may fail'
fi

missing_command=0
for command_name in curl docker ss stat grep python3; do
  if command -v "${command_name}" >/dev/null 2>&1; then
    pass "command available: ${command_name}"
  else
    fail "missing command: ${command_name}"
    missing_command=1
  fi
done

if [[ ${missing_command} -eq 0 ]]; then
  health_body=$(curl --silent --show-error --fail --max-time 12 \
    http://127.0.0.1:8600/healthz 2>/dev/null || true)
  if python3 -c 'import json,sys; data=json.load(sys.stdin); raise SystemExit(0 if data.get("ok") is True else 1)' \
      <<<"${health_body}" 2>/dev/null; then
    pass 'orchestrator healthz reports ok=true'
  else
    fail 'orchestrator healthz is unavailable or unhealthy'
  fi
  desktop_expected_open=$(python3 -c '
import json,sys
try:
    value=json.load(sys.stdin).get("desktop_access",{}).get("desired_open")
except Exception:
    value=None
print("true" if value is True else "false" if value is False else "unknown")
' <<<"${health_body}" 2>/dev/null || printf unknown)

  hydro_local_code=$(http_status 'http://127.0.0.1:8888/')
  if [[ "${hydro_local_code}" == '200' ]]; then
    pass 'local Hydro HTTP 200'
  else
    fail "local Hydro returned HTTP ${hydro_local_code}"
  fi

  exam_code=$(http_status "${EXAM_URL}/")
  if [[ "${exam_code}" == '200' ]]; then
    pass 'public exam entrance HTTP 200'
  else
    fail "public exam entrance returned HTTP ${exam_code}"
  fi

  hydro_code=$(http_status "${HYDRO_URL}/")
  if [[ "${hydro_code}" == '200' ]]; then
    pass 'public Hydro entrance HTTP 200'
  else
    fail "public Hydro entrance returned HTTP ${hydro_code}"
  fi

  submit_code=$(curl --silent --output /dev/null --max-time 12 \
    --write-out '%{http_code}' -X POST 'http://127.0.0.1:8888/orchestrator/submit' \
    -H 'Content-Type: application/json' --data '{}' 2>/dev/null || true)
  submit_code=${submit_code:-000}
  if [[ "${submit_code}" == '403' ]]; then
    pass 'Hydro submission endpoint rejects missing token with HTTP 403'
  else
    fail "Hydro submission guard returned HTTP ${submit_code}, expected 403"
  fi

  public_submit_code=$(curl --silent --output /dev/null --max-time 12 \
    --write-out '%{http_code}' -X POST "${HYDRO_URL}/orchestrator/submit" \
    --data '{}' 2>/dev/null || true)
  public_submit_code=${public_submit_code:-000}
  if [[ "${public_submit_code}" == '404' ]]; then
    pass 'public Hydro submission endpoint is hidden with HTTP 404'
  else
    fail "public Hydro submission endpoint returned HTTP ${public_submit_code}, expected 404"
  fi

  mongo_listeners=$(ss -ltnH '( sport = :27017 )' 2>/dev/null || true)
  if [[ -z "${mongo_listeners}" ]]; then
    fail 'MongoDB has no TCP listener on port 27017'
  elif grep -Eq '(^|[[:space:]])(0\.0\.0\.0|\*|\[::\]):27017([[:space:]]|$)' <<<"${mongo_listeners}"; then
    fail 'MongoDB is listening on a wildcard address'
  elif grep -Eq '127\.0\.0\.1:27017' <<<"${mongo_listeners}"; then
    pass 'MongoDB listens only on loopback'
  else
    fail 'MongoDB listener is not the expected loopback address'
  fi
fi

if docker inspect -f '{{.State.Running}}' "${ORCHESTRATOR_CONTAINER}" 2>/dev/null | grep -qx true; then
  pass "container running: ${ORCHESTRATOR_CONTAINER}"
  orchestrator_image=$(docker inspect -f '{{.Image}}' "${ORCHESTRATOR_CONTAINER}" 2>/dev/null || true)
  pass "orchestrator image: ${orchestrator_image:-unknown}"
else
  fail "container is not running: ${ORCHESTRATOR_CONTAINER}"
fi

pm2_bin=''
if [[ -x /root/.nix-profile/bin/pm2 ]]; then
  pm2_bin=/root/.nix-profile/bin/pm2
elif command -v pm2 >/dev/null 2>&1; then
  pm2_bin=$(command -v pm2)
fi

if [[ -z "${pm2_bin}" ]]; then
  fail 'PM2 executable not found'
else
  for process_name in caddy hydro-sandbox hydrooj mongodb; do
    process_pid=$("${pm2_bin}" pid "${process_name}" 2>/dev/null | grep -E '^[0-9]+$' | tail -n 1 || true)
    if [[ "${process_pid}" =~ ^[1-9][0-9]*$ ]]; then
      pass "PM2 online: ${process_name} pid=${process_pid}"
    else
      fail "PM2 process is offline: ${process_name}"
    fi
  done
fi

caddyfile=${CADDYFILE}
caddy_snippet="${PROJECT_ROOT}/orchestrator/runtime/caddy-exam.conf"
if [[ -f "${caddyfile}" ]] && grep -Fq "import ${caddy_snippet}" "${caddyfile}"; then
  pass 'Caddyfile imports the exam routing snippet'
else
  fail 'Caddyfile does not import the exam routing snippet'
fi

if [[ -s "${caddy_snippet}" ]]; then
  pass 'exam routing snippet exists and is non-empty'
else
  fail 'exam routing snippet is missing or empty'
fi

caddy_bin=${CADDY_BIN}
if [[ ! -x "${caddy_bin}" ]] && command -v caddy >/dev/null 2>&1; then
  caddy_bin=$(command -v caddy)
fi
json_fingerprint='import hashlib,json,sys; data=json.load(sys.stdin); encoded=json.dumps(data,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8"); print(hashlib.sha256(encoded).hexdigest())'
adapted_json_fingerprint='import hashlib,json,sys; envelope=json.load(sys.stdin); data=envelope.get("result") if isinstance(envelope,dict) and "warnings" in envelope and "result" in envelope else envelope; data=json.loads(data) if isinstance(data,str) else data; assert isinstance(data,dict); encoded=json.dumps(data,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8"); print(hashlib.sha256(encoded).hexdigest())'
if [[ ! -x "${caddy_bin}" ]]; then
  fail "Caddy executable not found: ${CADDY_BIN}"
elif [[ ! -s "${caddyfile}" ]]; then
  fail "Caddyfile is missing or empty: ${caddyfile}"
elif ! (
    cd "$(dirname -- "${caddyfile}")" \
      && "${caddy_bin}" validate --config "${caddyfile}" --adapter caddyfile \
        >/dev/null 2>&1
  ); then
  fail 'full Caddyfile on disk failed local validation'
elif ! disk_caddy_fingerprint=$(
    curl --silent --show-error --fail --max-time 12 -X POST \
      -H 'Content-Type: text/caddyfile' --data-binary "@${caddyfile}" \
      "${CADDY_ADMIN_URL%/}/adapt" 2>/dev/null \
      | python3 -c "${adapted_json_fingerprint}" 2>/dev/null
  ); then
  fail 'Caddy admin API could not adapt the full disk Caddyfile; live/disk comparison unavailable'
elif ! live_caddy_fingerprint=$(
    curl --silent --show-error --fail --max-time 12 \
      "${CADDY_ADMIN_URL%/}/config/" 2>/dev/null \
      | python3 -c "${json_fingerprint}"
  ); then
  fail 'Caddy admin config is unavailable; cannot prove the disk configuration is live'
elif [[ "${disk_caddy_fingerprint}" == "${live_caddy_fingerprint}" ]]; then
  pass 'Caddy live configuration matches the complete adapted disk Caddyfile'
else
  fail 'Caddy live/disk drift detected: active configuration differs from the admin-adapted disk Caddyfile'
fi

check_mode_600 /root/.hydro/orchestrator-token
plugin_env=/root/.hydro/orchestrator-plugin.env
check_mode_600 "${plugin_env}"
if [[ -r "${plugin_env}" ]]; then
  unset ORCHESTRATOR_IDEMPOTENCY_FILE \
    ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE \
    ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE \
    ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE
  set -a
  # shellcheck disable=SC1090
  if source "${plugin_env}"; then
    for state_variable in \
      ORCHESTRATOR_IDEMPOTENCY_FILE \
      ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE \
      ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE \
      ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE; do
      state_path=${!state_variable:-}
      if [[ -z "${state_path}" ]]; then
        fail "missing ${state_variable} in ${plugin_env}"
      else
        check_plugin_state_file "${state_path}"
      fi
    done
  else
    fail "cannot load plugin environment: ${plugin_env}"
  fi
  set +a
fi
check_mode_600 /root/noi-orchestrator-admin.txt
check_mode_600 "${PROJECT_ROOT}/orchestrator/.env"
check_mode_600 "${PROJECT_ROOT}/secrets/contest.pem"

cloud_status='UNKNOWN'
cloud_eip=''
if docker inspect -f '{{.State.Running}}' "${ORCHESTRATOR_CONTAINER}" 2>/dev/null | grep -qx true; then
  cloud_json=$(docker exec "${ORCHESTRATOR_CONTAINER}" python cloud_admin.py status 2>/dev/null || true)
  if cloud_status=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' \
      <<<"${cloud_json}" 2>/dev/null); then
    case "${cloud_status}" in
      Stopped|STOPPED)
        pass 'contest ECS is Stopped (StopCharging)'
        ;;
      Running|RUNNING)
        warn 'contest ECS is Running; confirm that a contest is active'
        ;;
      *)
        fail "contest ECS has unexpected status: ${cloud_status}"
        ;;
    esac
  else
    cloud_status='UNKNOWN'
    fail 'could not query contest ECS status'
  fi

  cloud_eip=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("eip", ""))' \
      <<<"${cloud_json}" 2>/dev/null || true)
  if [[ "${DESKTOP_ACCESS_MODE}" == direct ]]; then
    if CLOUD_EIP="${cloud_eip}" DESKTOP_PROBE_BASE_URL="${DESKTOP_PROBE_BASE_URL}" \
        python3 -c '
import ipaddress, os, sys
from urllib.parse import urlsplit
try:
    expected = ipaddress.ip_address(os.environ["CLOUD_EIP"])
    parsed = urlsplit(os.environ["DESKTOP_PROBE_BASE_URL"])
    actual = ipaddress.ip_address(parsed.hostname or "")
    port = parsed.port
except (ValueError, TypeError):
    raise SystemExit(1)
valid = (
    expected.version == 4
    and actual == expected
    and parsed.scheme == "http"
    and port in (None, 80)
    and parsed.username is None
    and parsed.password is None
    and parsed.path in ("", "/")
    and not parsed.query
    and not parsed.fragment
)
raise SystemExit(0 if valid else 1)
'; then
      pass 'direct desktop probe is pinned to the current contest EIP on raw HTTP/80'
    else
      fail 'DESKTOP_PROBE_BASE_URL is not the current contest EIP on raw HTTP/80'
    fi
  fi

  rules_json=$(docker exec "${ORCHESTRATOR_CONTAINER}" python cloud_admin.py rules 2>/dev/null || true)
  if EXPECTED_OJ_CIDR="${EXPECTED_OJ_CIDR}" \
      DESKTOP_ACCESS_MODE="${DESKTOP_ACCESS_MODE}" \
      DESKTOP_ACCESS_EXPECTED_OPEN="${desktop_expected_open:-unknown}" \
      STUDENT_CIDRS="${STUDENT_CIDRS}" python3 -c '
import json, os, sys
data = json.load(sys.stdin)
actual = [
    (
        str(r.get("protocol") or "").upper(),
        str(r.get("ports") or ""),
        str(r.get("source") or ""),
        str(r.get("policy") or "Accept").lower(),
    )
    for r in data.get("ingress", [])
]
expected = [
    ("TCP", "22/22", os.environ["EXPECTED_OJ_CIDR"], "accept"),
    ("TCP", "80/80", os.environ["EXPECTED_OJ_CIDR"], "accept"),
]
if os.environ["DESKTOP_ACCESS_MODE"] == "direct" and os.environ["DESKTOP_ACCESS_EXPECTED_OPEN"] == "true":
    cidrs = [item.strip() for item in os.environ["STUDENT_CIDRS"].split(",") if item.strip()]
    if len(cidrs) != 1:
        raise SystemExit(1)
    expected.append(("TCP", "80/80", cidrs[0], "accept"))
elif os.environ["DESKTOP_ACCESS_MODE"] == "direct" and os.environ["DESKTOP_ACCESS_EXPECTED_OPEN"] != "false":
    raise SystemExit(1)
raise SystemExit(0 if sorted(actual) == sorted(expected) else 1)
' <<<"${rules_json}" 2>/dev/null; then
    pass "security group matches ${DESKTOP_ACCESS_MODE} desktop access policy"
  else
    fail "contest ECS ingress rules differ from ${DESKTOP_ACCESS_MODE} policy"
  fi
fi

desktop_probe="${DESKTOP_PROBE_BASE_URL%/}/s/health-check/"
desktop_code=$(http_status "${desktop_probe}")
if [[ "${DESKTOP_ACCESS_MODE}" == direct ]]; then
  if [[ "${desktop_expected_open:-unknown}" == true && -n "${DESKTOP_PROBE_TOKEN}" ]]; then
    oj_valid_page="${EXAM_URL%/}/s/${DESKTOP_PROBE_TOKEN}/vnc.html?path=s/${DESKTOP_PROBE_TOKEN}/websockify&autoconnect=true&resize=scale&quality=${DESKTOP_PROBE_QUALITY}&compression=${DESKTOP_PROBE_COMPRESSION}&reconnect=true&reconnect_delay=5000"
    oj_valid_page_code=$(http_status "${oj_valid_page}")
    oj_valid_ws="${EXAM_URL%/}/s/${DESKTOP_PROBE_TOKEN}/websockify"
    oj_valid_ws_code=$(websocket_status "${oj_valid_ws}")
    if [[ "${oj_valid_page_code}" == 200 && "${oj_valid_ws_code}" == 101 ]] &&
        grep -Fq 'header_up Host {upstream_hostport}' "${caddy_snippet}" 2>/dev/null; then
      pass 'OJ-domain HTTPS compatibility desktop returned page 200 and WebSocket 101'
    else
      fail "direct mode HTTPS compatibility desktop failed (page=${oj_valid_page_code}, websocket=${oj_valid_ws_code})"
    fi
  else
    oj_desktop_code=$(http_status "${EXAM_URL%/}/s/health-check/")
    if [[ "${oj_desktop_code}" == 503 ]] &&
        grep -Fq 'respond "比赛桌面尚未开放" 503' "${caddy_snippet}" 2>/dev/null; then
      pass 'OJ-domain HTTPS compatibility desktop is closed outside the ready window'
    else
      fail "direct mode HTTPS compatibility desktop is not closed (HTTP ${oj_desktop_code})"
    fi
  fi
fi
if [[ "${DESKTOP_ACCESS_MODE}" == direct && "${desktop_expected_open:-unknown}" == true ]]; then
  if [[ -z "${DESKTOP_PROBE_TOKEN}" ]]; then
    fail 'direct desktop is expected open but DESKTOP_PROBE_TOKEN is empty'
  else
    valid_page="${DESKTOP_PROBE_BASE_URL%/}/s/${DESKTOP_PROBE_TOKEN}/vnc.html?path=s/${DESKTOP_PROBE_TOKEN}/websockify&autoconnect=true&resize=scale&quality=${DESKTOP_PROBE_QUALITY}&compression=${DESKTOP_PROBE_COMPRESSION}&reconnect=true&reconnect_delay=5000"
    valid_page_code=$(http_status "${valid_page}")
    valid_ws="${DESKTOP_PROBE_BASE_URL%/}/s/${DESKTOP_PROBE_TOKEN}/websockify"
    valid_ws_code=$(websocket_status "${valid_ws}")
    if [[ "${valid_page_code}" == 200 ]]; then
      pass 'valid direct noVNC seat page returned HTTP 200'
    else
      fail "valid direct noVNC seat page returned HTTP ${valid_page_code}"
    fi
    if [[ "${valid_ws_code}" == 101 ]]; then
      pass 'valid direct noVNC WebSocket upgraded with HTTP 101'
    else
      fail "valid direct noVNC WebSocket returned HTTP ${valid_ws_code}"
    fi
  fi
fi
case "${DESKTOP_ACCESS_MODE}:${cloud_status}:${desktop_code}" in
  proxy:Stopped:503|proxy:STOPPED:503)
    if grep -Fq 'respond "比赛桌面尚未开放" 503' "${caddy_snippet}" 2>/dev/null; then
      pass 'desktop entrance is closed with HTTP 503 while ECS is stopped'
    else
      fail 'desktop returns 503, but the expected closed-route configuration is missing'
    fi
    ;;
  direct:Stopped:000|direct:STOPPED:000)
    pass 'direct desktop endpoint is unreachable while ECS is stopped'
    ;;
  proxy:Running:404|proxy:RUNNING:404)
    if grep -Fq 'header_up Host {upstream_hostport}' "${caddy_snippet}" 2>/dev/null; then
      warn 'desktop endpoint reaches nginx and rejects an unknown seat token'
    else
      fail 'desktop endpoint is open, but safe upstream Host forwarding is missing'
    fi
    ;;
  direct:Running:404|direct:RUNNING:404)
    pass 'direct desktop endpoint reaches nginx and rejects an unknown seat token'
    ;;
  proxy:UNKNOWN:503)
    warn 'desktop entrance is closed, but ECS status could not be confirmed'
    ;;
  proxy:Stopped:502|proxy:STOPPED:502)
    if grep -Fq 'respond "比赛桌面尚未开放" 503' "${caddy_snippet}" 2>/dev/null; then
      fail 'desktop returned HTTP 502 although the disk snippet closes it with 503; Caddy is serving stale live configuration'
    else
      fail 'desktop returned HTTP 502 while ECS is stopped'
    fi
    ;;
  *)
    fail "desktop/ECS state mismatch: ECS=${cloud_status}, HTTP=${desktop_code}"
    ;;
esac

printf '\nsummary: pass=%d warn=%d fail=%d\n' "${pass_count}" "${warn_count}" "${fail_count}"
if [[ ${fail_count} -ne 0 ]]; then
  exit 1
fi
