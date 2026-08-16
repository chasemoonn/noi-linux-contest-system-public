#!/usr/bin/env bash
set -euo pipefail

caddyfile="${CADDYFILE:-/root/.hydro/Caddyfile}"
caddy="${CADDY_BIN:-/root/.nix-profile/bin/caddy}"
snippet=/opt/noi-linux-contest-system/orchestrator/runtime/caddy-exam.conf
import_line="import ${snippet}"
backup="${caddyfile}.noi-backup.$(date -u +%Y%m%dT%H%M%SZ)"
no_caddy_load=${NO_CADDY_LOAD:-0}
if [[ "${no_caddy_load}" != 0 && "${no_caddy_load}" != 1 ]]; then
    echo "NO_CADDY_LOAD must equal 0 or 1" >&2
    exit 2
fi

test -s "${caddyfile}"
test -x "${caddy}"
test -s "${snippet}"
cp -a "${caddyfile}" "${backup}"

rollback() {
    cp -a "${backup}" "${caddyfile}"
    if [[ "${no_caddy_load}" = 0 ]]; then
        curl -fsS -X POST -H 'Content-Type: text/caddyfile' \
            --data-binary "@${caddyfile}" http://127.0.0.1:2019/load \
            >/dev/null || true
    fi
}
trap 'rollback' ERR

if ! grep -Fqx -- "${import_line}" "${caddyfile}"; then
    printf '\n%s\n' "${import_line}" >> "${caddyfile}"
fi

"${caddy}" validate --config "${caddyfile}" --adapter caddyfile
if [[ "${no_caddy_load}" = 1 ]]; then
    trap - ERR
    echo "caddy_exam_candidate_ready path=${caddyfile} backup=${backup}"
    exit 0
fi
curl -fsS -X POST -H 'Content-Type: text/caddyfile' \
    --data-binary "@${caddyfile}" http://127.0.0.1:2019/load >/dev/null
curl -fsS http://127.0.0.1:8600/healthz >/dev/null

trap - ERR
echo "caddy_exam_enabled backup=${backup}"
