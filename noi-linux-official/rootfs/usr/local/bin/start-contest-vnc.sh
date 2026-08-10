#!/bin/bash
set -euo pipefail

USER_NAME="${STUDENT_USER:-student}"
RES="${RESOLUTION:-1366x768}"
FRAME_RATE="${FRAME_RATE:-30}"

if ! [[ "${FRAME_RATE}" =~ ^[0-9]+$ ]] \
    || (( FRAME_RATE < 10 || FRAME_RATE > 60 )); then
  echo "FRAME_RATE must be an integer between 10 and 60" >&2
  exit 1
fi

rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
exec su - "${USER_NAME}" -c \
  "vncserver :1 -fg -geometry '${RES}' -depth 24 -FrameRate '${FRAME_RATE}' -AlwaysShared -AcceptCutText -SendCutText -SendPrimary -SetPrimary -SecurityTypes VncAuth -localhost yes"
