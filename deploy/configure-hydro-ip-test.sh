#!/usr/bin/env bash
set -euo pipefail

test_ip="${NOI_TEST_IP:?set NOI_TEST_IP}"
caddyfile="${CADDYFILE:-/root/.hydro/Caddyfile}"
caddy="${CADDY_BIN:-/root/.nix-profile/bin/caddy}"
snippet=/opt/noi-linux-contest-system/orchestrator/runtime/caddy-ip-test.conf
import_line="import ${snippet}"
backup="${caddyfile}.noi-ip-backup.$(date -u +%Y%m%dT%H%M%SZ)"

python3 - "${test_ip}" <<'PY'
import ipaddress
import sys
ipaddress.ip_address(sys.argv[1])
PY

test -s "${caddyfile}"
test -x "${caddy}"
cp -a "${caddyfile}" "${backup}"

rollback() {
  cp -a "${backup}" "${caddyfile}"
  rm -f "${snippet}"
  curl -fsS -X POST -H 'Content-Type: text/caddyfile' \
    --data-binary "@${caddyfile}" http://127.0.0.1:2019/load \
    >/dev/null || true
}
trap 'rollback' ERR

cat > "${snippet}.tmp" <<EOF
# Temporary pre-DNS access for the NOI orchestrator.
http://${test_ip} {
    encode zstd gzip
    log {
        output file /root/.hydro/noi-exam-ip.access.log {
            roll_size 100mb
            roll_keep_for 24h
        }
        format json
    }
    handle {
        reverse_proxy http://127.0.0.1:8600
    }
}
EOF
chmod 0644 "${snippet}.tmp"
mv "${snippet}.tmp" "${snippet}"

if ! grep -Fqx -- "${import_line}" "${caddyfile}"; then
  printf '\n%s\n' "${import_line}" >> "${caddyfile}"
fi

"${caddy}" validate --config "${caddyfile}" --adapter caddyfile
curl -fsS -X POST -H 'Content-Type: text/caddyfile' \
  --data-binary "@${caddyfile}" http://127.0.0.1:2019/load >/dev/null

trap - ERR
echo "caddy_ip_test_enabled ip=${test_ip} backup=${backup}"
