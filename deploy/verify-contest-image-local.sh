#!/usr/bin/env bash
set -euo pipefail

image="${1:-noi-linux-official:2.0}"
expected_source_revision="${2:-}"
expected_iso_sha256='C8824240736352E5E4AAF3F6532B40961F75FA9F23D670BB78881355A49D5878'
expected_desktop_contract='finalizer-status-v1'

if [[ -n "${expected_source_revision}" \
    && ! "${expected_source_revision}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "期望源码 revision 必须是 40 位小写十六进制" >&2
    exit 2
fi

test_root="$(mktemp -d /tmp/noi-official-image-test.XXXXXX)"
test_suffix="${test_root##*.}"
folder_name="noi-official-folder-${test_suffix}"
web_name="noi-official-web-${test_suffix}"
both_name="noi-official-both-${test_suffix}"
containers=("${folder_name}" "${web_name}" "${both_name}")

cleanup() {
    docker rm -f "${containers[@]}" >/dev/null 2>&1 || true
    rm -rf -- "${test_root}"
}
trap cleanup EXIT

verify_firefox_policy() {
    local container="$1"
    docker exec "${container}" python3 -c '
import json
from pathlib import Path

paths = (
    Path("/etc/firefox/policies/policies.json"),
    Path("/usr/lib/firefox/distribution/policies.json"),
)
existing = [path for path in paths if path.exists() or path.is_symlink()]
if len(existing) != 1:
    raise SystemExit(f"expected exactly one Firefox policy source, found: {existing}")
path = existing[0]
if path.is_symlink() or not path.is_file():
    raise SystemExit(f"unsafe Firefox policy path: {path}")
try:
    document = json.loads(path.read_text(encoding="utf-8"))
    cookies = document["policies"]["Cookies"]
except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
    raise SystemExit(f"invalid Firefox policy document {path}: {exc}")
required = {"Default": True, "AcceptThirdParty": "never", "Locked": True}
if not isinstance(cookies, dict):
    raise SystemExit("policies.Cookies must be an object")
for key, expected in required.items():
    actual = cookies.get(key)
    if type(actual) is not type(expected) or actual != expected:
        raise SystemExit(
            f"unexpected policies.Cookies.{key}: {actual!r}"
        )
print(path)
'
}

if [[ -n "$(docker ps -q --filter label=noi.contest)" ]]; then
    echo "contest seat containers are running; image verification is refused" >&2
    exit 1
fi

image_id="$(docker image inspect "${image}" --format '{{.Id}}')"
image_iso_sha256="$(docker image inspect "${image_id}" \
    --format '{{index .Config.Labels "org.noi.iso.sha256"}}')"
if [[ "${image_iso_sha256}" != "${expected_iso_sha256}" ]]; then
    echo "镜像不是从已核验的官方 NOI Linux ISO 构建" >&2
    exit 1
fi
image_contract="$(docker image inspect "${image_id}" \
    --format '{{index .Config.Labels "org.noi.desktop.contract"}}')"
if [[ "${image_contract}" != "${expected_desktop_contract}" ]]; then
    echo "镜像不支持桌面就绪契约 ${expected_desktop_contract}" >&2
    exit 1
fi
image_source_revision="$(docker image inspect "${image_id}" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
if [[ ! "${image_source_revision}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "镜像缺少合法的 40 位小写源码 revision 标签" >&2
    exit 1
fi
if [[ -n "${expected_source_revision}" \
    && "${image_source_revision}" != "${expected_source_revision}" ]]; then
    echo "镜像源码 revision 与期望值不一致" >&2
    exit 1
fi

# From here onward use only the immutable ID that passed all label checks.
image="${image_id}"

for mode in folder web both; do
    case "${mode}" in
        folder) name="${folder_name}" ;;
        web) name="${web_name}" ;;
        both) name="${both_name}" ;;
    esac
    seat_root="${test_root}/${mode}"
    install -d -m 0755 \
        "${seat_root}/answers" \
        "${seat_root}/materials" \
        "${seat_root}/testdata/apple"
    base64 -d > "${seat_root}/materials/paper.pdf" <<'PDF_BASE64'
JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PCAvVHlwZSAvQ2F0YWxvZyAvUGFnZXMgMiAwIFIgPj4KZW5kb2JqCjIgMCBvYmoKPDwgL1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgPj4KZW5kb2JqCjMgMCBvYmoKPDwgL1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvTWVkaWFCb3ggWzAgMCAzMDAgMTQ0XSAvUmVzb3VyY2VzIDw8IC9Gb250IDw8IC9GMSA0IDAgUiA+PiA+PiAvQ29udGVudHMgNSAwIFIgPj4KZW5kb2JqCjQgMCBvYmoKPDwgL1R5cGUgL0ZvbnQgL1N1YnR5cGUgL1R5cGUxIC9CYXNlRm9udCAvSGVsdmV0aWNhID4+CmVuZG9iago1IDAgb2JqCjw8IC9MZW5ndGggNTMgPj4Kc3RyZWFtCkJUIC9GMSAxOCBUZiAzNiA4MCBUZCAoTk9JIGltYWdlIHZlcmlmaWNhdGlvbikgVGogRVQKZW5kc3RyZWFtCmVuZG9iagp4cmVmCjAgNgowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMTUgMDAwMDAgbiAKMDAwMDAwMDA2NCAwMDAwMCBuIAowMDAwMDAwMTIxIDAwMDAwIG4gCjAwMDAwMDAyNDcgMDAwMDAgbiAKMDAwMDAwMDMxNyAwMDAwMCBuIAp0cmFpbGVyCjw8IC9TaXplIDYgL1Jvb3QgMSAwIFIgPj4Kc3RhcnR4cmVmCjQxOQolJUVPRgo=
PDF_BASE64
    printf '1 2\n' > "${seat_root}/testdata/apple/1.in"
    printf '3\n' > "${seat_root}/testdata/apple/1.ans"
    chmod 0444 "${seat_root}/materials/paper.pdf" \
        "${seat_root}/testdata/apple/1.in" \
        "${seat_root}/testdata/apple/1.ans"

    extra=()
    if [[ "${mode}" != folder ]]; then
        extra=(-e WEB_SUBMIT_URL=http://172.20.0.1:18082/submit/test-token)
    fi
    docker run -d --name "${name}" \
        --memory 2560m --memory-swap 2560m --cpus 1.5 \
        --pids-limit 1024 --shm-size 1g \
        -e STUDENT_PASSWORD=testpass -e VNC_PASSWORD=testpass \
        -e CANDIDATE_ID=BJ0001 -e PROBLEM_NAMES=apple,banana \
        -e SUBMISSION_MODE="${mode}" -e HAS_TEST_DATA=1 \
        -e RESOLUTION=1366x768 -e FRAME_RATE=30 "${extra[@]}" \
        -v "${seat_root}/answers:/home/student/答案" \
        -v "${seat_root}/materials:/home/student/试题:ro" \
        -v "${seat_root}/testdata:/home/student/测试数据:ro" \
        "${image}" >/dev/null
done

for _attempt in $(seq 1 90); do
    ready=0
    for name in "${containers[@]}"; do
        if docker exec "${name}" grep -Fqx ready \
            /home/student/.contest-finalizer-status >/dev/null 2>&1 \
            && docker exec "${name}" pgrep -x gnome-shell >/dev/null 2>&1 \
            && docker exec "${name}" curl -fsS --max-time 3 \
                http://127.0.0.1:6080/vnc.html >/dev/null 2>&1; then
            ready=$((ready + 1))
        fi
    done
    [[ "${ready}" -eq 3 ]] && break
    sleep 2
done

for name in "${containers[@]}"; do
    docker exec "${name}" grep -Fqx ready \
        /home/student/.contest-finalizer-status
    docker exec "${name}" pgrep -x systemd-logind >/dev/null
    docker exec "${name}" pgrep -f gnome-session-binary >/dev/null
    docker exec "${name}" pgrep -x gnome-shell >/dev/null
    docker exec "${name}" pgrep -x ibus-daemon >/dev/null
    verify_firefox_policy "${name}"
    docker exec "${name}" sh -lc \
        'firefox --version | grep -Fq "Mozilla Firefox 79."'
    docker exec "${name}" sh -lc '
        pid="$(pgrep -xo Xtigervnc)" &&
        args="$(tr "\000" " " < "/proc/${pid}/cmdline")" &&
        for expected in "-FrameRate 30" -AcceptCutText -SendCutText -SendPrimary -SetPrimary; do
            printf "%s\n" "${args}" | grep -Fq -- "${expected}" || exit 1
        done
    '
    docker exec "${name}" test -L \
        '/home/student/比赛资料（从这里开始）'
    docker exec "${name}" test -L \
        '/home/student/Desktop/比赛资料（从这里开始）'
    docker exec "${name}" sh -lc \
        "test \"\$(readlink -f '/home/student/比赛资料（从这里开始）')\" = /run/contest-materials"
    docker exec "${name}" grep -Fqx schema=2 \
        '/run/contest-materials/.manifest'
    docker exec "${name}" test -r \
        '/run/contest-materials/00_请先看.txt'
    docker exec "${name}" test -r \
        '/run/contest-materials/01_试题.pdf'
    docker exec -u student "${name}" sh -lc '
        command -v evince >/dev/null
        handler="$(xdg-mime query default application/pdf)"
        test -n "${handler}"
        gio mime application/pdf | grep -Fq "${handler}"
    '
    docker exec "${name}" test -d \
        '/run/contest-materials/02_测试数据'
    docker exec "${name}" test -L '/home/student/Desktop/试题.pdf'
    docker exec "${name}" test -L '/home/student/Desktop/测试数据'
    docker exec "${name}" test -L \
        '/home/student/Desktop/答案文件夹（自动回收）'
    docker exec -u student "${name}" test ! -w \
        '/run/contest-materials'
    docker exec -u student "${name}" test -w \
        '/run/contest-materials/03_答案文件夹（自动回收）'
    docker exec -u student "${name}" test -w \
        '/home/student/答案/BJ0001/apple'
    docker exec -u student "${name}" test -w \
        '/home/student/答案/BJ0001/banana'
    docker exec -u student "${name}" test ! -w \
        '/home/student/试题/paper.pdf'
    docker exec -u student "${name}" test ! -w \
        '/home/student/测试数据/apple/1.in'
    docker exec -u student -e HOME=/home/student -e DISPLAY=:1 \
        -e XAUTHORITY=/home/student/.Xauthority "${name}" sh -lc \
        "setxkbmap -query | grep -Eq '^layout:[[:space:]]+us$'"
    docker exec -u student -e HOME=/home/student -e DISPLAY=:1 \
        -e XAUTHORITY=/home/student/.Xauthority "${name}" sh -lc '
        uid="$(id -u)"
        session_pid="$(pgrep -u "${uid}" -o -x gnome-shell)"
        DBUS_SESSION_BUS_ADDRESS="$(tr "\000" "\n" \
            < "/proc/${session_pid}/environ" |
            sed -n "s/^DBUS_SESSION_BUS_ADDRESS=//p" | head -n1)"
        XDG_RUNTIME_DIR="$(tr "\000" "\n" \
            < "/proc/${session_pid}/environ" |
            sed -n "s/^XDG_RUNTIME_DIR=//p" | head -n1)"
        test -n "${DBUS_SESSION_BUS_ADDRESS}"
        export DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR
        test "$(gsettings get org.gnome.desktop.background picture-uri)" = \
            "'\''file:///usr/share/backgrounds/noi_wallpaper_00.png'\''"
        gsettings get org.gnome.desktop.input-sources sources | grep -Fq libpinyin
        ibus list-engine | grep -Fq libpinyin
        ibus engine libpinyin
        test "$(ibus engine)" = libpinyin
        ibus engine xkb:us::eng
        test "$(ibus engine)" = xkb:us::eng
    '
done

docker exec "${folder_name}" test ! -e \
    '/home/student/Desktop/CSP 程序回收系统.desktop'
docker exec "${folder_name}" test ! -e \
    '/run/contest-materials/04_CSP程序回收系统.html'
for name in "${web_name}" "${both_name}"; do
    docker exec "${name}" test -x \
        '/home/student/Desktop/CSP 程序回收系统.desktop'
    docker exec "${name}" test -r \
        '/run/contest-materials/04_CSP程序回收系统.html'
    docker exec "${name}" sh -lc '
        launcher="/home/student/Desktop/CSP 程序回收系统.desktop"
        grep -Eq "^Exec=firefox --new-window https?://" "${launcher}"
        ! grep -Eq "^Exec=.*--(no-remote|profile)([=[:space:]]|$)" "${launcher}"
    '
    docker exec -u student -e HOME=/home/student "${name}" sh -lc '
        uid="$(id -u)"
        session_pid="$(pgrep -u "${uid}" -o -x gnome-shell)"
        DBUS_SESSION_BUS_ADDRESS="$(tr "\000" "\n" \
            < "/proc/${session_pid}/environ" |
            sed -n "s/^DBUS_SESSION_BUS_ADDRESS=//p" | head -n1)"
        XDG_RUNTIME_DIR="$(tr "\000" "\n" \
            < "/proc/${session_pid}/environ" |
            sed -n "s/^XDG_RUNTIME_DIR=//p" | head -n1)"
        test -n "${DBUS_SESSION_BUS_ADDRESS}"
        export DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR
        gio info "$HOME/Desktop/CSP 程序回收系统.desktop" |
            grep -Fq "metadata::trusted: true"
    '
done

# Exercise the real Firefox 79 cookie engine, not merely the JSON source. The
# probe verifies a first-party HttpOnly/SameSite=Strict cookie across a 303,
# rejects a third-party cookie, and survives a normal-mode browser restart.
# The launcher is checked separately to ensure it does not isolate profiles.
docker exec -u student -e HOME=/home/student "${web_name}" \
    python3 /usr/local/bin/verify-firefox-cookie-runtime.py

# A student can modify their container home before a restart. Prove that the
# root entrypoint preserves conflicting paths, recreates real managed parent
# directories, and refuses to follow a problem-directory symlink.
docker exec -u student -e HOME=/home/student "${folder_name}" sh -lc '
    for path in .config .vnc Desktop; do
        mv -- "$HOME/${path}" "$HOME/${path}.before-restart"
        ln -s "$HOME/答案" "$HOME/${path}"
    done
    rmdir "$HOME/答案/BJ0001/banana"
    ln -s "$HOME/答案/BJ0001/apple" "$HOME/答案/BJ0001/banana"
'
docker restart "${folder_name}" >/dev/null
for _attempt in $(seq 1 90); do
    if docker exec "${folder_name}" grep -Fqx ready \
        /home/student/.contest-finalizer-status >/dev/null 2>&1 \
        && docker exec "${folder_name}" pgrep -x gnome-shell >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
docker exec "${folder_name}" grep -Fqx ready \
    /home/student/.contest-finalizer-status
for path in .config .vnc Desktop; do
    docker exec "${folder_name}" test -d "/home/student/${path}"
    docker exec "${folder_name}" test ! -L "/home/student/${path}"
    docker exec "${folder_name}" test -d "/home/student/${path}.before-restart"
    docker exec "${folder_name}" test -L "/home/student/${path}.student-backup"
done
docker exec "${folder_name}" test -d '/home/student/答案/BJ0001/banana'
docker exec "${folder_name}" test ! -L '/home/student/答案/BJ0001/banana'
docker exec "${folder_name}" test -L \
    '/home/student/答案/BJ0001/banana.student-backup'
docker exec "${folder_name}" test -L \
    '/home/student/比赛资料（从这里开始）'
docker exec "${folder_name}" test -L \
    '/home/student/Desktop/比赛资料（从这里开始）'
docker exec "${folder_name}" test -L '/home/student/Desktop/试题.pdf'
docker exec "${folder_name}" test -L '/home/student/Desktop/测试数据'
docker exec "${folder_name}" test -L \
    '/home/student/Desktop/答案文件夹（自动回收）'
docker exec "${folder_name}" grep -Fqx schema=2 \
    '/run/contest-materials/.manifest'
docker exec "${folder_name}" test -r '/run/contest-materials/01_试题.pdf'
docker exec "${folder_name}" test -d '/run/contest-materials/02_测试数据'
docker exec -u student "${folder_name}" test ! -w \
    '/home/student/试题/paper.pdf'
docker exec -u student "${folder_name}" test ! -w \
    '/home/student/测试数据/apple/1.in'
docker exec -u student "${folder_name}" test -w \
    '/run/contest-materials/03_答案文件夹（自动回收）'
docker exec "${folder_name}" test ! -e \
    '/home/student/Desktop/CSP 程序回收系统.desktop'

docker exec "${folder_name}" g++ --version | head -n1
docker exec "${folder_name}" sh -lc 'command -v firefox'
docker image inspect "${image}" \
    --format 'image={{.Id}} size={{.Size}} created={{.Created}}'
echo official_image_modes_verified
