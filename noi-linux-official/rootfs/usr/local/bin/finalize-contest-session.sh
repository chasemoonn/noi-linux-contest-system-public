#!/bin/bash
set -euo pipefail

status_file="${HOME}/.contest-finalizer-status"
finalizer_ready=0
finalizer_exit() {
    rc=$?
    trap - EXIT
    if [[ "${finalizer_ready}" != "1" ]]; then
        [[ "${rc}" -ne 0 ]] || rc=1
        printf 'failed:%s\n' "${rc}" > "${status_file}" || true
    fi
    exit "${rc}"
}
trap finalizer_exit EXIT
printf 'starting\n' > "${status_file}"

# GNOME, IBus and GVfs do not become ready at exactly the same time. Keep all
# finalization work inside one bounded retry loop so one early gsettings error
# cannot skip launcher trust or keyboard setup.
sleep 2
ready=0
for _attempt in $(seq 1 40); do
    input_ready=1
    gsettings set org.gnome.desktop.input-sources sources \
        "[('xkb', 'us'), ('ibus', 'libpinyin')]" || input_ready=0
    gsettings set org.gnome.desktop.input-sources mru-sources \
        "[('xkb', 'us'), ('ibus', 'libpinyin')]" || input_ready=0
    gsettings set org.gnome.desktop.input-sources current 'uint32 0' \
        || input_ready=0
    gsettings set org.gnome.desktop.wm.keybindings switch-input-source \
        "['<Super>space', '<Ctrl>space']" || input_ready=0
    gsettings set org.gnome.desktop.wm.keybindings switch-input-source-backward \
        "['<Shift><Super>space', '<Shift><Ctrl>space']" || input_ready=0
    setxkbmap -layout us -option '' || input_ready=0
    if ! pgrep -u "$(id -u)" -x ibus-daemon >/dev/null 2>&1; then
        ibus-daemon --daemonize --xim --panel disable || input_ready=0
    fi
    ibus engine xkb:us::eng >/dev/null 2>&1 || input_ready=0

    gsettings set org.gnome.nautilus.preferences executable-text-activation \
        'launch' || input_ready=0
    all_trusted=1
    for launcher in "${HOME}/Desktop/"*.desktop; do
        [[ -f "${launcher}" ]] || continue
        chmod 0755 "${launcher}"
        if gio set -t string "${launcher}" metadata::trusted true \
            && gio info "${launcher}" | grep -Fq 'metadata::trusted: true'; then
            # DING notices the refreshed mtime and redraws the launcher icon.
            touch "${launcher}"
        else
            all_trusted=0
        fi
    done
    if [[ "${input_ready}" == "1" && "${all_trusted}" == "1" ]]; then
        ready=1
        break
    fi
    sleep 1
done
if [[ "${ready}" == "1" ]]; then
    printf 'ready\n' > "${status_file}"
    finalizer_ready=1
    exit 0
fi
exit 1
