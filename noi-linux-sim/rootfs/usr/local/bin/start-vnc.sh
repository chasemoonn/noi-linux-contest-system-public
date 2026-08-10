#!/bin/bash
set -euo pipefail

USER_NAME="${STUDENT_USER:-student}"
RES="${RESOLUTION:-1600x900}"

rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
su - "${USER_NAME}" -c \
  "vncserver :1 -geometry '${RES}' -depth 24 -AlwaysShared -SecurityTypes VncAuth -localhost yes"

exec tail -F "/home/${USER_NAME}/.vnc/"*.log

