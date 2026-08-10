#!/usr/bin/env bash
set -euo pipefail

caddyfile="${CADDYFILE:-/root/.hydro/Caddyfile}"
caddy="${CADDY_BIN:-/root/.nix-profile/bin/caddy}"
snippet=/opt/noi-linux-contest-system/orchestrator/runtime/caddy-exam.conf
import_line="import ${snippet}"
backup="${caddyfile}.noi-backup.$(date -u +%Y%m%dT%H%M%SZ)"

test -s "${caddyfile}"
test -x "${caddy}"
test -s "${snippet}"
cp -a "${caddyfile}" "${backup}"

rollback() {
    cp -a "${backup}" "${caddyfile}"
    curl -fsS -X POST -H 'Content-Type: text/caddyfile' \
        --data-binary "@${caddyfile}" http://127.0.0.1:2019/load \
        >/dev/null || true
}
trap 'rollback' ERR

if ! grep -Fqx -- "${import_line}" "${caddyfile}"; then
    printf '\n%s\n' "${import_line}" >> "${caddyfile}"
fi

"${caddy}" validate --config "${caddyfile}" --adapter caddyfile
curl -fsS -X POST -H 'Content-Type: text/caddyfile' \
    --data-binary "@${caddyfile}" http://127.0.0.1:2019/load >/dev/null
curl -fsS http://127.0.0.1:8600/healthz >/dev/null

trap - ERR
echo "caddy_exam_enabled backup=${backup}"
