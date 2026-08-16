#!/bin/bash
set -euo pipefail

USER_NAME=student
: "${STUDENT_PASSWORD:?STUDENT_PASSWORD is required}"
PASS="${STUDENT_PASSWORD}"
VNC_PASS="${VNC_PASSWORD:-${PASS}}"
export RESOLUTION="${RESOLUTION:-1366x768}"
export STUDENT_USER="${USER_NAME}"
HOME_DIR="/home/${USER_NAME}"
CANDIDATE_ID="${CANDIDATE_ID:-U0000}"
PROBLEM_NAMES="${PROBLEM_NAMES:-}"
SUBMISSION_MODE="${SUBMISSION_MODE:-both}"
WEB_SUBMIT_URL="${WEB_SUBMIT_URL:-}"
HAS_TEST_DATA="${HAS_TEST_DATA:-0}"
BUNDLE_DIR="/run/contest-materials"

preserve_conflict() {
    local path="$1"
    local backup="${path}.student-backup"
    local suffix=0
    while [[ -e "${backup}" || -L "${backup}" ]]; do
        suffix=$((suffix + 1))
        backup="${path}.student-backup-${suffix}"
    done
    mv -- "${path}" "${backup}"
    printf 'preserved conflicting path: %s -> %s\n' "${path}" "${backup}" >&2
}

prepare_managed_path() {
    local path="$1"
    if [[ -L "${path}" || -f "${path}" ]]; then
        rm -f -- "${path}"
    elif [[ -e "${path}" ]]; then
        preserve_conflict "${path}"
    fi
}

ensure_symlink() {
    local target="$1"
    local link="$2"
    prepare_managed_path "${link}"
    ln -s -- "${target}" "${link}"
}

ensure_real_directory() {
    local path="$1"
    local owner="$2"
    local group="$3"
    local mode="$4"
    if [[ -L "${path}" || ( -e "${path}" && ! -d "${path}" ) ]]; then
        preserve_conflict "${path}"
    fi
    install -d -o "${owner}" -g "${group}" -m "${mode}" -- "${path}"
    if [[ -L "${path}" || ! -d "${path}" ]]; then
        echo "managed path is not a real directory: ${path}" >&2
        exit 1
    fi
}

require_real_directory() {
    local path="$1"
    if [[ -L "${path}" || ! -d "${path}" ]]; then
        echo "required mount is not a real directory: ${path}" >&2
        exit 1
    fi
}

if [[ ! "${CANDIDATE_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]; then
    echo "invalid CANDIDATE_ID" >&2
    exit 1
fi
if [[ "${SUBMISSION_MODE}" != "both" ]]; then
    echo "V1 requires the unified formal-directory submission contract" >&2
    exit 1
fi
if [[ ! "${HAS_TEST_DATA}" =~ ^[01]$ ]]; then
    echo "invalid HAS_TEST_DATA" >&2
    exit 1
fi

if ! id "${USER_NAME}" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "${USER_NAME}"
fi
printf '%s:%s\n' "${USER_NAME}" "${PASS}" | chpasswd

ensure_real_directory "${HOME_DIR}/.vnc" "${USER_NAME}" "${USER_NAME}" 0700
prepare_managed_path "${HOME_DIR}/.vnc/passwd"
printf '%s\n' "${VNC_PASS}" | vncpasswd -f > "${HOME_DIR}/.vnc/passwd"
chmod 0600 "${HOME_DIR}/.vnc/passwd"

prepare_managed_path "${HOME_DIR}/.vnc/xstartup"
cat > "${HOME_DIR}/.vnc/xstartup" <<'EOF'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP=ubuntu:GNOME
export XDG_SESSION_DESKTOP=ubuntu
export GNOME_SHELL_SESSION_MODE=ubuntu
export LANG=zh_CN.UTF-8
export LANGUAGE=zh_CN:zh
export LC_ALL=zh_CN.UTF-8
export GTK_IM_MODULE=ibus
export QT_IM_MODULE=ibus
export XMODIFIERS=@im=ibus
export LIBGL_ALWAYS_SOFTWARE=1
setxkbmap -layout us -option '' || true
exec dbus-run-session -- gnome-session --session=ubuntu
EOF
chmod 0755 "${HOME_DIR}/.vnc/xstartup"

ensure_real_directory "${HOME_DIR}/Desktop" "${USER_NAME}" "${USER_NAME}" 0755
ensure_real_directory "${HOME_DIR}/.config" "${USER_NAME}" "${USER_NAME}" 0755
ensure_real_directory "${HOME_DIR}/.config/autostart" \
    "${USER_NAME}" "${USER_NAME}" 0755
prepare_managed_path "${HOME_DIR}/.contest-finalizer-status"
prepare_managed_path "${HOME_DIR}/.config/gnome-initial-setup-done"
touch "${HOME_DIR}/.config/gnome-initial-setup-done"
prepare_managed_path \
    "${HOME_DIR}/.config/autostart/gnome-initial-setup-first-login.desktop"
cat > "${HOME_DIR}/.config/autostart/gnome-initial-setup-first-login.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=GNOME Initial Setup
Hidden=true
EOF
prepare_managed_path "${HOME_DIR}/.config/autostart/ibus-contest.desktop"
cat > "${HOME_DIR}/.config/autostart/ibus-contest.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=IBus Contest Input Method
Exec=ibus-daemon --daemonize --replace --xim --panel disable
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
prepare_managed_path "${HOME_DIR}/.config/autostart/contest-materials.desktop"
cat > "${HOME_DIR}/.config/autostart/contest-materials.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=比赛资料
Exec=nautilus --new-window /home/student/Desktop
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
prepare_managed_path "${HOME_DIR}/.config/user-dirs.dirs"
cat > "${HOME_DIR}/.config/user-dirs.dirs" <<'EOF'
XDG_DESKTOP_DIR="$HOME/Desktop"
XDG_DOWNLOAD_DIR="$HOME/Downloads"
XDG_DOCUMENTS_DIR="$HOME/Documents"
XDG_PICTURES_DIR="$HOME/Pictures"
XDG_MUSIC_DIR="$HOME/Music"
XDG_VIDEOS_DIR="$HOME/Videos"
XDG_TEMPLATES_DIR="$HOME/Templates"
XDG_PUBLICSHARE_DIR="$HOME/Public"
EOF

ANSWER_ROOT="${HOME_DIR}/答案"
require_real_directory "${ANSWER_ROOT}"
require_real_directory "${HOME_DIR}/试题"
if [[ "${HAS_TEST_DATA}" == "1" ]]; then
    require_real_directory "${HOME_DIR}/测试数据"
fi
ANSWER_ROOT_REAL="$(realpath -e -- "${ANSWER_ROOT}")"
ANSWER_DIR="${ANSWER_ROOT}/${CANDIDATE_ID}"
ensure_real_directory "${ANSWER_DIR}" "${USER_NAME}" "${USER_NAME}" 0755
if [[ "$(realpath -e -- "${ANSWER_DIR}")" != "${ANSWER_ROOT_REAL}/${CANDIDATE_ID}" ]]; then
    echo "candidate answer directory escaped the mounted answer root" >&2
    exit 1
fi
IFS=',' read -r -a problems <<< "${PROBLEM_NAMES}"
EXAMPLE_PROBLEM=""
for problem in "${problems[@]}"; do
    [[ -z "${problem}" ]] && continue
    if [[ ! "${problem}" =~ ^[a-z][a-z0-9_]{0,63}$ ]]; then
        echo "invalid problem name: ${problem}" >&2
        exit 1
    fi
    ensure_real_directory "${ANSWER_DIR}/${problem}" \
        "${USER_NAME}" "${USER_NAME}" 0755
    if [[ "$(realpath -e -- "${ANSWER_DIR}/${problem}")" \
        != "${ANSWER_ROOT_REAL}/${CANDIDATE_ID}/${problem}" ]]; then
        echo "problem answer directory escaped the mounted answer root" >&2
        exit 1
    fi
    [[ -n "${EXAMPLE_PROBLEM}" ]] || EXAMPLE_PROBLEM="${problem}"
done
EXAMPLE_PROBLEM="${EXAMPLE_PROBLEM:-problem}"

ensure_symlink "${ANSWER_DIR}" "${HOME_DIR}/submit"

for managed_path in \
    "${HOME_DIR}/Desktop/answers.desktop" \
    "${HOME_DIR}/Desktop/web-submit.desktop" \
    "${HOME_DIR}/Desktop/CSP 程序回收系统.desktop" \
    "${HOME_DIR}/Desktop/答案文件夹（自动回收）" \
    "${HOME_DIR}/提交方式.txt"; do
    prepare_managed_path "${managed_path}"
done
if [[ ! "${WEB_SUBMIT_URL}" =~ ^https?://[A-Za-z0-9._:/?\&=%-]+$ ]]; then
    echo "invalid WEB_SUBMIT_URL" >&2
    exit 1
fi

find "${HOME_DIR}/Desktop" -maxdepth 1 -type f -name '*.desktop' -exec chmod 0755 {} +
rm -f /etc/machine-id
dbus-uuidgen --ensure=/etc/machine-id
install -d -m 0755 /run/dbus
rm -f /run/dbus/pid /run/dbus/system_bus_socket
# The paper and test-data directories are read-only bind mounts. Never recurse
# into them while fixing ownership or the container would fail before the
# desktop starts.
find "${HOME_DIR}" \( -path "${HOME_DIR}/试题" -o -path "${HOME_DIR}/测试数据" \) -prune -o \
    -exec chown -h "${USER_NAME}:${USER_NAME}" {} +
for managed_path in \
    "${HOME_DIR}/Desktop/paper.desktop" \
    "${HOME_DIR}/Desktop/试题.pdf" \
    "${HOME_DIR}/Desktop/测试数据" \
    "${HOME_DIR}/Desktop/答案文件夹（自动回收）" \
    "${HOME_DIR}/Desktop/01_比赛题面.pdf" \
    "${HOME_DIR}/Desktop/02_辅助自测数据" \
    "${HOME_DIR}/Desktop/03_答案文件夹" \
    "${HOME_DIR}/Desktop/04_CSP程序回收系统.html" \
    "${HOME_DIR}/Desktop/05_使用说明.txt"; do
    prepare_managed_path "${managed_path}"
done
if [[ "${HAS_TEST_DATA}" != "1" ]]; then
    echo "V1 contest contract requires approved practice data" >&2
    exit 1
fi

# The canonical bundle is root-owned. Student-writable home paths only contain
# replaceable symlinks, so a stale directory or symlink cannot redirect the
# root entrypoint or prevent the seat from becoming ready after a restart.
install -d -o root -g root -m 0755 "${BUNDLE_DIR}"
for managed_path in \
    "${BUNDLE_DIR}/01_比赛题面.pdf" \
    "${BUNDLE_DIR}/02_辅助自测数据" \
    "${BUNDLE_DIR}/03_答案文件夹" \
    "${BUNDLE_DIR}/04_CSP程序回收系统.html" \
    "${BUNDLE_DIR}/05_使用说明.txt" \
    "${BUNDLE_DIR}/.manifest"; do
    prepare_managed_path "${managed_path}"
done
ensure_symlink "${HOME_DIR}/试题/paper.pdf" "${BUNDLE_DIR}/01_比赛题面.pdf"
ensure_symlink "${HOME_DIR}/测试数据" "${BUNDLE_DIR}/02_辅助自测数据"
ensure_symlink "${ANSWER_DIR}" "${BUNDLE_DIR}/03_答案文件夹"
cat > "${BUNDLE_DIR}/04_CSP程序回收系统.html" <<EOF
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url=${WEB_SUBMIT_URL}">
  <title>CSP 程序回收系统</title>
</head>
<body>
  <p>正在打开 CSP 程序回收系统……</p>
  <p><a href="${WEB_SUBMIT_URL}">如果没有自动打开，请点击这里</a></p>
</body>
</html>
EOF
cat > "${BUNDLE_DIR}/05_使用说明.txt" <<EOF
准考证号：${CANDIDATE_ID}

桌面五个入口的用途：
1. 01_比赛题面.pdf：本场 CSP 风格题面。
2. 02_辅助自测数据：每题 2～4 组 .in/.out，只供自测，不参与评分。
3. 03_答案文件夹：唯一正式代码目录。
   例如：03_答案文件夹/${EXAMPLE_PROBLEM}/${EXAMPLE_PROBLEM}.cpp
   系统只读取规定的 .cpp 源程序；编译生成的 .bin、.in、.out 等文件不参与评分。
4. 04_CSP程序回收系统.html：从正式代码目录读取并递交到 OJ 系统。
5. 05_使用说明.txt：本说明。

每次点击递交都会在 OJ 系统产生一条评测记录。保存文件不等于递交；
只有页面显示“已送入 OJ 系统”才表示该次递交已确认。截止时系统会自动
冻结 03_答案文件夹，并以截止时最后文件作为最终计分版本。

输入下划线：先按 Ctrl+Space 切到英文，再按 Shift+-。
短文本可用远程桌面左侧工具栏的剪贴板按钮。
EOF
cat > "${BUNDLE_DIR}/.manifest" <<EOF
schema=3
candidate_id=${CANDIDATE_ID}
has_test_data=${HAS_TEST_DATA}
EOF
find "${BUNDLE_DIR}" -type f -exec chmod 0644 {} +
for name in \
    '01_比赛题面.pdf' \
    '02_辅助自测数据' \
    '03_答案文件夹' \
    '04_CSP程序回收系统.html' \
    '05_使用说明.txt'; do
    ensure_symlink "${BUNDLE_DIR}/${name}" "${HOME_DIR}/Desktop/${name}"
    chown -h "${USER_NAME}:${USER_NAME}" "${HOME_DIR}/Desktop/${name}"
done
ensure_symlink "${BUNDLE_DIR}" "${HOME_DIR}/比赛资料（从这里开始）"
ensure_symlink "${BUNDLE_DIR}" "${HOME_DIR}/Desktop/比赛资料（从这里开始）"
chown -h "${USER_NAME}:${USER_NAME}" \
    "${HOME_DIR}/比赛资料（从这里开始）"
chown -h "${USER_NAME}:${USER_NAME}" \
    "${HOME_DIR}/Desktop/比赛资料（从这里开始）"
su - "${USER_NAME}" -c "dbus-run-session -- /usr/local/bin/configure-contest-desktop.sh"

exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/contest.conf
