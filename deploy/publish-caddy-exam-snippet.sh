#!/usr/bin/env bash
set -euo pipefail

candidate=${1:?usage: publish-caddy-exam-snippet.sh CANDIDATE [SNIPPET]}
snippet=${2:-/opt/noi-linux-contest-system/orchestrator/runtime/caddy-exam.conf}
caddyfile=${CADDYFILE:-/root/.hydro/Caddyfile}
caddy=${CADDY_BIN:-/root/.nix-profile/bin/caddy}
admin_load_url=${CADDY_ADMIN_LOAD_URL:-http://127.0.0.1:2019/load}
backup=${CADDY_SNIPPET_BACKUP:-${snippet}.before-install}
import_line="import ${snippet}"

test -s "${candidate}"
test -s "${caddyfile}"
test -x "${caddy}"

snippet_dir=$(dirname -- "${snippet}")
install -d -m 0755 "${snippet_dir}"
temporary=$(mktemp "${snippet_dir}/.caddy-exam.conf.publish.XXXXXX")
validation_file=''

cleanup() {
    rm -f -- "${temporary}"
    if [[ -n "${validation_file}" ]]; then
        rm -f -- "${validation_file}"
    fi
}
trap cleanup EXIT

load_full_config() {
    "${caddy}" validate --config "${caddyfile}" --adapter caddyfile
    # Caddy's /load endpoint swaps the complete active configuration only after
    # the submitted Caddyfile has adapted and provisioned successfully.
    curl -fsS -X POST -H 'Content-Type: text/caddyfile' \
        --data-binary "@${caddyfile}" "${admin_load_url}" >/dev/null
}

restore_previous_snippet() {
    install -m 0644 "${backup}" "${temporary}"
    mv -Tf -- "${temporary}" "${snippet}"
    if ! load_full_config; then
        printf '%s\n' \
            'CRITICAL: restored the previous Caddy snippet on disk, but reloading it failed' \
            >&2
        return 1
    fi
    printf '%s\n' 'previous Caddy snippet restored and reloaded' >&2
}

if [[ -s "${snippet}" ]]; then
    backup_dir=$(dirname -- "${backup}")
    if [[ ! -d "${backup_dir}" ]]; then
        install -d -m 0700 "${backup_dir}"
    fi
    cp -a -- "${snippet}" "${backup}"
fi

if ! grep -Fqx -- "${import_line}" "${caddyfile}"; then
    # On a first installation the exam site is enabled later by the dedicated
    # configure script. Validate the complete prospective Caddyfile now, but do
    # not change the live configuration before the import is deliberately added.
    validation_file=$(mktemp "$(dirname -- "${caddyfile}")/.Caddyfile.noi-validate.XXXXXX")
    cp -a -- "${caddyfile}" "${validation_file}"
    printf '\nimport %s\n' "${candidate}" >> "${validation_file}"
    "${caddy}" validate --config "${validation_file}" --adapter caddyfile
    install -m 0644 "${candidate}" "${temporary}"
    mv -Tf -- "${temporary}" "${snippet}"
    printf 'caddy_exam_snippet_staged path=%s (import not active)\n' "${snippet}"
    exit 0
fi

# An active import without a restorable on-disk snippet is already inconsistent.
# Refuse to replace it because a failed publish could not meet the rollback
# guarantee or reconstruct the configuration currently held in Caddy memory.
if [[ ! -s "${snippet}" ]]; then
    printf 'active Caddy import has no restorable snippet: %s\n' "${snippet}" >&2
    exit 1
fi

install -m 0644 "${candidate}" "${temporary}"
mv -Tf -- "${temporary}" "${snippet}"

if ! load_full_config; then
    printf '%s\n' \
        'Caddy publish failed; restoring the previous exam snippet and full live configuration' \
        >&2
    restore_previous_snippet
    exit 1
fi

printf 'caddy_exam_snippet_published path=%s backup=%s\n' \
    "${snippet}" "${backup}"
