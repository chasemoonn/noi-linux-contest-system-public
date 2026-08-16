#!/usr/bin/env bash
set -euo pipefail

: "${CADDYFILE:?set CADDYFILE to the active Hydro Caddyfile}"
: "${HYDRO_DOMAIN:?set HYDRO_DOMAIN to the public Hydro DNS name}"
: "${CONFIRM_HARDEN_SUBMIT_ROUTE:?set CONFIRM_HARDEN_SUBMIT_ROUTE=YES after reviewing the target}"
if [[ "${CONFIRM_HARDEN_SUBMIT_ROUTE}" != YES ]]; then
  printf 'CONFIRM_HARDEN_SUBMIT_ROUTE must equal YES\n' >&2
  exit 2
fi
if [[ ! "${HYDRO_DOMAIN}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] ||
    [[ "${HYDRO_DOMAIN}" != *.* ]] || [[ "${HYDRO_DOMAIN}" == *..* ]]; then
  printf 'HYDRO_DOMAIN must be a bare DNS name without scheme, port, path, or wildcard\n' >&2
  exit 2
fi
caddyfile=${CADDYFILE}
hydro_domain=${HYDRO_DOMAIN}
caddy=${CADDY_BIN:-/root/.nix-profile/bin/caddy}
caddy_admin_url=${CADDY_ADMIN_URL:-http://127.0.0.1:2019}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="${caddyfile}.submit-route-backup.${timestamp}"
no_caddy_load=${NO_CADDY_LOAD:-0}
if [[ "${no_caddy_load}" != 0 && "${no_caddy_load}" != 1 ]]; then
  printf 'NO_CADDY_LOAD must equal 0 or 1\n' >&2
  exit 2
fi

test -s "${caddyfile}"
test -x "${caddy}"
cp -a "${caddyfile}" "${backup}"

rollback() {
  cp -a "${backup}" "${caddyfile}"
  if [[ "${no_caddy_load}" = 0 ]]; then
    curl -fsS -X POST -H 'Content-Type: text/caddyfile' \
      --data-binary "@${caddyfile}" "${caddy_admin_url%/}/load" >/dev/null || true
  fi
}
trap 'rollback' ERR

python3 - "${caddyfile}" ':80' "${hydro_domain}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
targets = sys.argv[2:]
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
marker = "# noi-orchestrator-private-submit"
insertions = []
found = set()

depth = 0
for index, line in enumerate(lines):
    stripped = line.strip()
    if depth == 0:
        for target in targets:
            if stripped == f"{target} {{":
                found.add(target)
                block_depth = 0
                end = index
                for end in range(index, len(lines)):
                    block_depth += lines[end].count("{") - lines[end].count("}")
                    if block_depth == 0:
                        break
                block = "".join(lines[index : end + 1])
                if marker not in block:
                    indent = line[: len(line) - len(line.lstrip())] + "  "
                    insertions.append(
                        (
                            index + 1,
                            [
                                f"{indent}{marker}\n",
                                f"{indent}handle /orchestrator/submit* {{\n",
                                f"{indent}  respond 404\n",
                                f"{indent}}}\n",
                            ],
                        )
                    )
    depth += line.count("{") - line.count("}")

missing = set(targets) - found
if missing:
    raise SystemExit(f"Caddy site block not found: {', '.join(sorted(missing))}")

for index, content in reversed(insertions):
    lines[index:index] = content

temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text("".join(lines), encoding="utf-8")
temporary.replace(path)
PY

"${caddy}" validate --config "${caddyfile}" --adapter caddyfile
if [[ "${no_caddy_load}" = 1 ]]; then
  trap - ERR
  echo "hydro_submit_route_candidate_ready path=${caddyfile} backup=${backup}"
  exit 0
fi
curl -fsS -X POST -H 'Content-Type: text/caddyfile' \
  --data-binary "@${caddyfile}" "${caddy_admin_url%/}/load" >/dev/null

public_status=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  "https://${hydro_domain}/orchestrator/submit")
local_status=$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  http://127.0.0.1:8888/orchestrator/submit)
test "${public_status}" = 404
test "${local_status}" = 403

trap - ERR
echo "hydro_submit_route_private public=${public_status} local=${local_status} backup=${backup}"
