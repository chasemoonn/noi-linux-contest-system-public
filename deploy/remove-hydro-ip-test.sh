#!/usr/bin/env bash
set -euo pipefail

caddyfile="${CADDYFILE:-/root/.hydro/Caddyfile}"
caddy="${CADDY_BIN:-/root/.nix-profile/bin/caddy}"
snippet=/opt/noi-linux-contest-system/orchestrator/runtime/caddy-ip-test.conf
import_line="import ${snippet}"
backup="${caddyfile}.noi-ip-remove-backup.$(date -u +%Y%m%dT%H%M%SZ)"
snippet_backup="${snippet}.remove-backup"

test -s "${caddyfile}"
test -x "${caddy}"
cp -a "${caddyfile}" "${backup}"
if [[ -f "${snippet}" ]]; then
  cp -a "${snippet}" "${snippet_backup}"
fi

rollback() {
  cp -a "${backup}" "${caddyfile}"
  if [[ -f "${snippet_backup}" ]]; then
    mv "${snippet_backup}" "${snippet}"
  fi
  curl -fsS -X POST -H 'Content-Type: text/caddyfile' \
    --data-binary "@${caddyfile}" http://127.0.0.1:2019/load \
    >/dev/null || true
}
trap 'rollback' ERR

python3 - "${caddyfile}" "${import_line}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
target = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()
path.write_text(
    "\n".join(line for line in lines if line.strip() != target) + "\n",
    encoding="utf-8",
)
PY
rm -f "${snippet}"

"${caddy}" validate --config "${caddyfile}" --adapter caddyfile
curl -fsS -X POST -H 'Content-Type: text/caddyfile' \
  --data-binary "@${caddyfile}" http://127.0.0.1:2019/load >/dev/null
rm -f "${snippet_backup}"

trap - ERR
echo "caddy_ip_test_removed backup=${backup}"
