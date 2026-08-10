#!/bin/bash
set -euo pipefail

USER_NAME=student
PASS="${STUDENT_PASSWORD:-noilinux123}"
VNC_PASS="${VNC_PASSWORD:-${PASS}}"
export RESOLUTION="${RESOLUTION:-1600x900}"
export STUDENT_USER="${USER_NAME}"
HOME_DIR="/home/${USER_NAME}"
CANDIDATE_ID="${CANDIDATE_ID:-U0000}"
PROBLEM_NAMES="${PROBLEM_NAMES:-}"
SUBMISSION_MODE="${SUBMISSION_MODE:-folder}"
WEB_SUBMIT_URL="${WEB_SUBMIT_URL:-}"

if [[ ! "${CANDIDATE_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]; then
    echo "invalid CANDIDATE_ID" >&2
    exit 1
fi
if [[ ! "${SUBMISSION_MODE}" =~ ^(folder|web|both)$ ]]; then
    echo "invalid SUBMISSION_MODE" >&2
    exit 1
fi

if ! id "${USER_NAME}" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "${USER_NAME}"
fi
printf '%s:%s\n' "${USER_NAME}" "${PASS}" | chpasswd

mkdir -p "${HOME_DIR}/.vnc"
printf '%s\n' "${VNC_PASS}" | vncpasswd -f > "${HOME_DIR}/.vnc/passwd"
chmod 0600 "${HOME_DIR}/.vnc/passwd"

cat > "${HOME_DIR}/.vnc/xstartup" <<'EOF'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
export GTK_IM_MODULE=ibus
export QT_IM_MODULE=ibus
export XMODIFIERS=@im=ibus
ibus-daemon -drx
exec startxfce4
EOF
chmod 0755 "${HOME_DIR}/.vnc/xstartup"

cp -rn /opt/template/. "${HOME_DIR}/" 2>/dev/null || true
mkdir -p "${HOME_DIR}/.config/geany/filedefs"
cp -n /opt/geany/filetypes.cpp "${HOME_DIR}/.config/geany/filedefs/" 2>/dev/null || true
ANSWER_DIR="${HOME_DIR}/答案/${CANDIDATE_ID}"
mkdir -p "${ANSWER_DIR}" "${HOME_DIR}/题目"
IFS=',' read -r -a problems <<< "${PROBLEM_NAMES}"
for problem in "${problems[@]}"; do
    [[ -z "${problem}" ]] && continue
    if [[ ! "${problem}" =~ ^[a-z][a-z0-9_]{0,63}$ ]]; then
        echo "invalid problem name: ${problem}" >&2
        exit 1
    fi
    mkdir -p "${ANSWER_DIR}/${problem}"
done

if [[ -e "${HOME_DIR}/submit" && ! -L "${HOME_DIR}/submit" ]]; then
    mv "${HOME_DIR}/submit" "${HOME_DIR}/submit.legacy"
fi
ln -sfn "${ANSWER_DIR}" "${HOME_DIR}/submit"

rm -f "${HOME_DIR}/Desktop/web-submit.desktop"
if [[ "${SUBMISSION_MODE}" =~ ^(web|both)$ ]]; then
    if [[ ! "${WEB_SUBMIT_URL}" =~ ^https?://[A-Za-z0-9._:/?\&=%-]+$ ]]; then
        echo "invalid WEB_SUBMIT_URL" >&2
        exit 1
    fi
    cat >"${HOME_DIR}/Desktop/web-submit.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=网页递交（北京模式）
Comment=只用于递交已在编辑器中完成的源代码
Exec=epiphany-browser --new-window ${WEB_SUBMIT_URL}
Icon=web-browser
Terminal=false
EOF
fi

cat >"${HOME_DIR}/提交方式.txt" <<EOF
本场提交方式：${SUBMISSION_MODE}
准考证号（答案目录名）：${CANDIDATE_ID}
答案目录：${ANSWER_DIR}

folder：比赛结束自动回收答案目录。
web：使用桌面“网页递交（北京模式）”，每题以网页最后一次提交为准。
both：网页为正式提交；答案文件夹同时回收作为备份。若某题没有网页提交，使用文件夹版本。
EOF

find "${HOME_DIR}/Desktop" -maxdepth 1 -type f -name '*.desktop' -exec chmod 0755 {} +
chown -R "${USER_NAME}:${USER_NAME}" "${HOME_DIR}"

exec /usr/bin/supervisord -n -c /etc/supervisor/supervisord.conf
